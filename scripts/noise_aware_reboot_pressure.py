# -*- coding: utf-8 -*-
"""Noise-aware reboot pressure runner for ARCS offline voice validation.

Serial data and tool execution records are saved to separate realtime logs.
Both logs flush every line so interrupted runs still leave usable evidence.
"""

from __future__ import annotations

import argparse
import base64
import datetime as _dt
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

import serial

try:
    import pygame
except ImportError as exc:  # pragma: no cover - runtime dependency
    raise SystemExit("ERROR: pygame is required for default soundcard playback") from exc

PORT_DEFAULT = "COM28"
BAUD_DEFAULT = 921600
VOLUME_DEFAULT = 60
WAKE_FILE_DEFAULT = "你好小奥.wav"
OPEN_FILE_DEFAULT = "打开浴霸.wav"
CLOSE_FILE_DEFAULT = "关闭浴霸.wav"

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
MODE1_RE = re.compile(r"wakeup switch mode to:\s*1|wakeup_algo_switch_mode_cb:\s*mode=1|\bMODE=1\b", re.I)
TIMEOUT_RE = re.compile(r"wakeup timeout|wakeup_algo_timeout_cb|wakeup switch mode to:\s*0|wakeup_algo_switch_mode_cb:\s*mode=0|\bMODE=0\b", re.I)
WAKE_RE = re.compile(r"WAKE\(1\).*ni3 hao3 xiao3 ao4|keyword\":\"ni3 hao3 xiao3 ao4", re.I)
OPEN_RE = re.compile(r"Recognized offline command:\s*kai yu ba|Offline command submitted:\s*kai yu ba", re.I)
CLOSE_RE = re.compile(r"Recognized offline command:\s*guan yu ba|Offline command submitted:\s*guan yu ba", re.I)
BOOT_RE = re.compile(r"wakeup_algo|ListenAI:/\$", re.I)


def now() -> str:
    return _dt.datetime.now().isoformat(timespec="milliseconds")


def now_dt() -> _dt.datetime:
    return _dt.datetime.now()


def xshell_ts(dt: Optional[_dt.datetime] = None) -> str:
    dt = dt or now_dt()
    return f"{dt.year}/{dt.month}/{dt.day} {dt:%H:%M:%S}.{dt.microsecond // 1000:03d}"


def xshell_ts_seconds(dt: Optional[_dt.datetime] = None) -> str:
    dt = dt or now_dt()
    return f"{dt.year}/{dt.month}/{dt.day} {dt:%H:%M:%S}"


def parse_local_datetime(value: Optional[str]) -> Optional[_dt.datetime]:
    if not value:
        return None
    normalized = value.strip().replace(" ", "T")
    parsed = _dt.datetime.fromisoformat(normalized)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def xshell_line(text: str = "") -> str:
    return f"[{xshell_ts()}] {text}\r\n"


def port_label(port: str) -> str:
    digits = "".join(ch for ch in port if ch.isdigit())
    if digits:
        return f"COM{int(digits):03d}"
    return port


def clean_ansi(line: str) -> str:
    return ANSI_RE.sub("", line).replace("\r", "")


def stable_serial_text(text: str) -> str:
    """Keep each serial record on one physical log line."""
    return text.replace("\r", "\\r")


def escaped_serial_data(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    return (
        text.replace("\\", "\\\\")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
        .replace("\x1b", "\\x1b")
    )


def ts_line(text: str) -> str:
    return f"[{now()}] {text}\n"


class TerminalLineParser:
    """Small terminal-style parser matching Xshell-readable line logging."""

    def __init__(self) -> None:
        self.current = ""
        self.ansi_tail = ""

    def feed(self, data: bytes):
        text = self.ansi_tail + data.decode("utf-8", errors="replace")
        self.ansi_tail = ""
        last_esc = text.rfind("\x1b")
        if last_esc != -1 and not ANSI_RE.match(text[last_esc:]) and not re.search(r"\x1b\[[0-?]*[ -/]*[@-~]$", text[last_esc:]):
            self.ansi_tail = text[last_esc:]
            text = text[:last_esc]
        text = ANSI_RE.sub("", text).replace("\x1b", "")
        lines = []
        i = 0
        while i < len(text):
            ch = text[i]
            if ch == "\r":
                if i + 1 < len(text) and text[i + 1] == "\n":
                    pass
                else:
                    self.current = ""
            elif ch == "\n":
                lines.append(self.current)
                self.current = ""
            else:
                self.current += ch
            i += 1
        return lines

    def flush(self):
        if self.current:
            line = self.current
            self.current = ""
            return [line]
        return []


def load_config(path: str) -> dict:
    config_path = Path(path)
    if not config_path.exists():
        return {}
    return json.loads(config_path.read_text(encoding="utf-8-sig"))


def cfg_get(config: dict, dotted: str, default=None):
    value = config
    for key in dotted.split("."):
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    return value


def cfg_bool(config: dict, dotted: str, default: bool) -> bool:
    value = cfg_get(config, dotted, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def apply_config(args: argparse.Namespace) -> argparse.Namespace:
    config = load_config(args.config)
    args.port = args.port or cfg_get(config, "serial.port", PORT_DEFAULT)
    args.baud = args.baud or int(cfg_get(config, "serial.baud", BAUD_DEFAULT))
    args.audio_dir = args.audio_dir or cfg_get(config, "audio.dir", "wav")
    args.wake_file = args.wake_file or cfg_get(config, "audio.wake_file", WAKE_FILE_DEFAULT)
    args.open_file = args.open_file or cfg_get(config, "audio.open_file", OPEN_FILE_DEFAULT)
    args.close_file = args.close_file or cfg_get(config, "audio.close_file", CLOSE_FILE_DEFAULT)
    args.out_dir = args.out_dir or cfg_get(config, "logging.out_dir", "result")
    args.volume = args.volume if args.volume is not None else int(cfg_get(config, "playback.volume", VOLUME_DEFAULT))
    args.max_wake_retries = (
        args.max_wake_retries
        if args.max_wake_retries is not None
        else int(cfg_get(config, "pressure.max_wake_retries_on_command_miss", 6))
    )
    args.retry_until_timeout = (
        args.retry_until_timeout
        if args.retry_until_timeout is not None
        else cfg_bool(config, "pressure.retry_until_timeout", True)
    )
    args.wake_retry_gap = (
        args.wake_retry_gap
        if args.wake_retry_gap is not None
        else float(cfg_get(config, "pressure.wake_retry_gap_seconds", 0.8))
    )
    args.timeout_wait = (
        args.timeout_wait
        if args.timeout_wait is not None
        else float(cfg_get(config, "pressure.timeout_wait_seconds", 35.0))
    )
    if not args.no_fsync:
        args.no_fsync = not bool(cfg_get(config, "logging.fsync", True))
    args.end_at_dt = parse_local_datetime(args.end_at)
    return args


class RealtimeTaskLog:
    """Realtime log that flushes every write to preserve live evidence."""

    def __init__(self, path: Path, fsync: bool = True, encoding: str = "utf-8") -> None:
        self.path = path
        self.fsync = fsync
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("w", encoding=encoding, errors="replace", newline="", buffering=1)

    def write(self, line: str) -> None:
        self.handle.write(line)
        self.handle.flush()
        if self.fsync:
            os.fsync(self.handle.fileno())

    def close(self) -> None:
        self.handle.flush()
        if self.fsync:
            os.fsync(self.handle.fileno())
        self.handle.close()

    def __enter__(self) -> "RealtimeTaskLog":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()


def require_tool(name: str) -> str:
    tool = shutil.which(name)
    if not tool:
        raise RuntimeError(f"{name} not found in PATH")
    return tool


def run_command(command, *, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        capture_output=True,
        text=text,
        encoding="utf-8" if text else None,
        errors="replace" if text else None,
        check=False,
    )


def set_windows_default_volume(percent: int) -> None:
    """Set Windows default render endpoint volume without selecting another device."""
    if platform.system().lower() != "windows":
        return
    percent = max(0, min(100, int(percent)))
    scalar = percent / 100.0
    ps = rf'''
$code = @"
using System;
using System.Runtime.InteropServices;
public class DefaultAudioVolume {{
  enum EDataFlow {{ eRender = 0, eCapture = 1, eAll = 2 }}
  enum ERole {{ eConsole = 0, eMultimedia = 1, eCommunications = 2 }}
  [ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class MMDeviceEnumerator {{}}
  [Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IMMDeviceEnumerator {{
    int EnumAudioEndpoints(EDataFlow dataFlow, uint dwStateMask, IntPtr ppDevices);
    int GetDefaultAudioEndpoint(EDataFlow dataFlow, ERole role, out IMMDevice ppDevice);
    int GetDevice(string pwstrId, out IMMDevice ppDevice);
    int RegisterEndpointNotificationCallback(IntPtr pClient);
    int UnregisterEndpointNotificationCallback(IntPtr pClient);
  }}
  [Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IMMDevice {{
    int Activate(ref Guid iid, uint dwClsCtx, IntPtr pActivationParams, out IAudioEndpointVolume ppInterface);
    int OpenPropertyStore(uint stgmAccess, IntPtr ppProperties);
    int GetId(out IntPtr ppstrId);
    int GetState(out uint pdwState);
  }}
  [Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IAudioEndpointVolume {{
    int RegisterControlChangeNotify(IntPtr pNotify);
    int UnregisterControlChangeNotify(IntPtr pNotify);
    int GetChannelCount(out uint pnChannelCount);
    int SetMasterVolumeLevel(float fLevelDB, ref Guid pguidEventContext);
    int SetMasterVolumeLevelScalar(float fLevel, ref Guid pguidEventContext);
    int GetMasterVolumeLevel(out float pfLevelDB);
    int GetMasterVolumeLevelScalar(out float pfLevel);
    int SetChannelVolumeLevel(uint nChannel, float fLevelDB, ref Guid pguidEventContext);
    int SetChannelVolumeLevelScalar(uint nChannel, float fLevel, ref Guid pguidEventContext);
    int GetChannelVolumeLevel(uint nChannel, out float pfLevelDB);
    int GetChannelVolumeLevelScalar(uint nChannel, out float pfLevel);
    int SetMute([MarshalAs(UnmanagedType.Bool)] bool bMute, ref Guid pguidEventContext);
    int GetMute(out bool pbMute);
    int GetVolumeStepInfo(out uint pnStep, out uint pnStepCount);
    int VolumeStepUp(ref Guid pguidEventContext);
    int VolumeStepDown(ref Guid pguidEventContext);
    int QueryHardwareSupport(out uint pdwHardwareSupportMask);
    int GetVolumeRange(out float pflVolumeMindB, out float pflVolumeMaxdB, out float pflVolumeIncrementdB);
  }}
  static IAudioEndpointVolume Endpoint() {{
    var enumerator = (IMMDeviceEnumerator)(new MMDeviceEnumerator());
    IMMDevice device;
    int hr = enumerator.GetDefaultAudioEndpoint(EDataFlow.eRender, ERole.eMultimedia, out device);
    if (hr != 0) Marshal.ThrowExceptionForHR(hr);
    Guid iid = typeof(IAudioEndpointVolume).GUID;
    IAudioEndpointVolume volume;
    hr = device.Activate(ref iid, 23, IntPtr.Zero, out volume);
    if (hr != 0) Marshal.ThrowExceptionForHR(hr);
    return volume;
  }}
  public static void Set(float scalar) {{
    var volume = Endpoint();
    Guid ctx = Guid.Empty;
    int hr = volume.SetMasterVolumeLevelScalar(scalar, ref ctx);
    if (hr != 0) Marshal.ThrowExceptionForHR(hr);
    hr = volume.SetMute(false, ref ctx);
    if (hr != 0) Marshal.ThrowExceptionForHR(hr);
  }}
}}
"@
Add-Type -TypeDefinition $code -Language CSharp
[DefaultAudioVolume]::Set([single]{scalar})
'''
    encoded = base64.b64encode(ps.encode("utf-16le")).decode("ascii")
    result = run_command(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded])
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "failed to set default volume").strip()
        raise RuntimeError(details)


def normalize_audio(source: Path, target: Path) -> None:
    ffmpeg = require_tool("ffmpeg")
    target.parent.mkdir(parents=True, exist_ok=True)
    result = run_command([
        ffmpeg,
        "-y",
        "-v",
        "error",
        "-i",
        str(source),
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ac",
        "2",
        "-ar",
        "44100",
        str(target),
    ])
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "ffmpeg normalize failed").strip()
        raise RuntimeError(details)


def make_clean_log(raw_log: Path) -> Path:
    clean_log = raw_log.with_suffix(".clean.log")
    with raw_log.open("r", encoding="utf-8", errors="replace") as src, clean_log.open(
        "w", encoding="utf-8", errors="replace", newline=""
    ) as dst:
        for line in src:
            dst.write(clean_ansi(line))
    return clean_log


class Runner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        start_dt = _dt.datetime.now()
        stamp = start_dt.strftime("%Y%m%d_%H%M%S")
        xshell_stamp = start_dt.strftime("%Y-%m-%d_%H_%M_%S")
        self.out_dir = Path(args.out_dir) / stamp
        self.norm_dir = self.out_dir / "normalized"
        self.start_dt = start_dt
        self.serial_log_path = self.out_dir / f"{port_label(args.port)}_{args.baud}_{xshell_stamp}.log"
        run_label = f"until_{args.end_at_dt:%Y%m%d_%H%M%S}" if args.end_at_dt else f"{args.cycles}cycles"
        self.tool_log_path = self.out_dir / f"noise_aware_reboot_pressure_{run_label}.tool.log"
        self.summary_path = self.out_dir / f"noise_aware_reboot_pressure_{run_label}.summary.json"
        self.q: queue.Queue[Tuple[str, str]] = queue.Queue()
        self.stop = threading.Event()
        self.records = []
        self.serial_chunk_count = 0
        self.serial_byte_count = 0
        self.serial_log: Optional[RealtimeTaskLog] = None
        self.tool_log: Optional[RealtimeTaskLog] = None
        self.sounds: Dict[str, pygame.mixer.Sound] = {}
        self.summary = {
            "config": {
                "port": args.port,
                "baud": args.baud,
                "cycles": args.cycles,
                "end_at": args.end_at_dt.isoformat(timespec="seconds") if args.end_at_dt else None,
                "volume": None if args.skip_volume else args.volume,
                "soundcard": "Windows default render device only; no devicename/device-key",
                "serial_log_policy": "Xshell-compatible readable serial log with [BEGIN]/[END] and per-line timestamps",
                "tool_log_policy": "tool execution events in a separate realtime log; every line has millisecond timestamp",
                "noise_policy": "first-window unified wake/open/close probe; target fail requires no wake and no command until timeout, then recovery check",
                "retry_until_timeout": args.retry_until_timeout,
                "max_wake_retries_on_command_miss": args.max_wake_retries,
                "wake_retry_gap_seconds": args.wake_retry_gap,
                "timeout_wait_seconds": args.timeout_wait,
            },
            "files": {},
            "cycles": [],
        }

    def audio_paths(self) -> Dict[str, Path]:
        audio_dir = Path(self.args.audio_dir)
        return {
            "wake": audio_dir / self.args.wake_file,
            "open": audio_dir / self.args.open_file,
            "close": audio_dir / self.args.close_file,
        }

    def prepare(self) -> None:
        if not self.args.skip_volume:
            self.mark("SET_DEFAULT_VOLUME_BEGIN", str(self.args.volume))
            set_windows_default_volume(self.args.volume)
            self.mark("SET_DEFAULT_VOLUME_DONE", str(self.args.volume))
        else:
            self.mark("SET_DEFAULT_VOLUME_SKIP", "skip_volume=true")
        paths = self.audio_paths()
        for label, path in paths.items():
            if not path.exists():
                raise FileNotFoundError(path)
            self.mark("AUDIO_SOURCE_FOUND", f"{label}={path}")
        normalized = {
            "wake": self.norm_dir / "wake_nihao_xiaoao_44k_stereo.wav",
            "open": self.norm_dir / "cmd_open_yuba_44k_stereo.wav",
            "close": self.norm_dir / "cmd_close_yuba_44k_stereo.wav",
        }
        for label, target in normalized.items():
            self.mark("AUDIO_NORMALIZE_BEGIN", f"{label} -> {target}")
            normalize_audio(paths[label], target)
            self.mark("AUDIO_NORMALIZE_DONE", f"{label} bytes={target.stat().st_size}")
        os.environ["SDL_AUDIODRIVER"] = "directsound" if platform.system().lower() == "windows" else os.environ.get("SDL_AUDIODRIVER", "")
        self.mark("PYGAME_MIXER_INIT_BEGIN", f"SDL_AUDIODRIVER={os.environ.get('SDL_AUDIODRIVER', '')}")
        pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=1024, allowedchanges=0)
        self.mark("PYGAME_MIXER_INIT_DONE", "frequency=44100 size=-16 channels=2 buffer=1024")
        self.sounds = {label: pygame.mixer.Sound(str(path)) for label, path in normalized.items()}
        for sound in self.sounds.values():
            sound.set_volume(1.0)
        self.mark("PYGAME_SOUND_LOAD_DONE", ",".join(sorted(self.sounds.keys())))
        self.summary["files"] = {
            "serial_log": str(self.serial_log_path),
            "tool_log": str(self.tool_log_path),
            "summary": str(self.summary_path),
            **{f"normalized_{label}": str(path) for label, path in normalized.items()},
        }

    def serial_reader(self, ser: serial.Serial) -> None:
        line_parser = TerminalLineParser()
        chunk_seq = 0
        byte_total = 0
        while not self.stop.is_set():
            try:
                data = ser.read(4096)
            except Exception as exc:  # pylint: disable=broad-except
                self.q.put(("tool", ts_line("SERIAL_READ_ERROR " + repr(exc))))
                break
            if not data:
                continue
            chunk_seq += 1
            byte_total += len(data)
            lines = line_parser.feed(data)
            self.q.put(
                (
                    "tool",
                    ts_line(
                        f"SERIAL_READ chunk={chunk_seq} bytes={len(data)} total_bytes={byte_total} emitted_lines={len(lines)}"
                    ),
                )
            )
            for line in lines:
                clean_line = clean_ansi(line)
                self.q.put(("serial_line", xshell_line(clean_line)))
                self.q.put(("analysis", clean_line))
        for line in line_parser.flush():
            clean_line = clean_ansi(line)
            self.q.put(("serial_line", xshell_line(clean_line)))
            self.q.put(("analysis", clean_line))

    def drain(self) -> None:
        assert self.serial_log is not None
        while True:
            try:
                kind, line = self.q.get_nowait()
            except queue.Empty:
                break
            if kind == "serial_line":
                self.serial_log.write(line)
            elif kind == "analysis":
                self.records.append(line)
            elif self.tool_log is not None:
                self.tool_log.write(line if line.startswith("[") else ts_line(line.rstrip("\n")))

    def mark(self, label: str, detail: str = "") -> None:
        assert self.tool_log is not None
        self.tool_log.write(ts_line(f"EVENT {label} {detail}"))
        print(now(), label, detail, flush=True)

    def wait(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            self.drain()
            time.sleep(0.025)
        self.drain()

    def wait_for(self, label: str, regex: re.Pattern, timeout: float, start: Optional[int] = None) -> Tuple[bool, Optional[int]]:
        if start is None:
            start = len(self.records)
        self.mark("WAIT_BEGIN:" + label, f"timeout={timeout}s start_analysis_index={start} pattern={regex.pattern}")
        end = time.time() + timeout
        while time.time() < end:
            self.drain()
            for idx, line in enumerate(self.records[start:], start):
                if regex.search(line):
                    self.mark("WAIT_MATCH:" + label, line.strip()[:260])
                    return True, idx
            time.sleep(0.04)
        self.mark("WAIT_TIMEOUT:" + label, f"timeout={timeout}s")
        return False, None

    def count_since(self, start: int, regex: re.Pattern) -> int:
        self.drain()
        return sum(1 for line in self.records[start:] if regex.search(line))

    def play(self, label: str, sound_key: str) -> int:
        self.drain()
        start = len(self.records)
        t0 = time.time()
        self.mark("PLAY_START:" + label, "")
        channel = self.sounds[sound_key].play()
        if channel is None:
            raise RuntimeError("pygame failed to start playback for " + label)
        while channel.get_busy():
            self.drain()
            time.sleep(0.02)
        self.drain()
        self.mark("PLAY_END:" + label, f"duration_s={time.time() - t0:.3f} analysis_start_index={start}")
        return start

    @staticmethod
    def command_regex(name: str) -> re.Pattern:
        return OPEN_RE if name == "open" else CLOSE_RE

    def timeout_seen_since(self, start: int) -> bool:
        self.drain()
        return any(TIMEOUT_RE.search(line) for line in self.records[start:])

    def first_window_unified_probe(self, cycle: int, window_start: int) -> Dict[str, object]:
        """Probe both wake-again and commands inside the first recognition window."""
        probe: Dict[str, object] = {
            "probe_rounds": 0,
            "wake_detected_count": 0,
            "open_recognized_count": 0,
            "close_recognized_count": 0,
            "command_recognized_count": 0,
            "full_pass_before_timeout": False,
            "any_activity_before_timeout": False,
            "timeout_seen": False,
            "safety_deadline_hit": False,
            "target_failure_candidate": False,
            "recovery_mode1_after_timeout": None,
            "recovery_open_recognized_after_timeout": None,
            "recovery_close_recognized_after_timeout": None,
            "classification": "unknown",
            "rounds": [],
        }
        self.mark(
            f"CYCLE_{cycle}_FIRST_WINDOW_UNIFIED_PROBE_BEGIN",
            "repeat wake/open/close until full pass, any activity, timeout, or safety deadline",
        )
        deadline = time.monotonic() + self.args.timeout_wait
        while True:
            if self.timeout_seen_since(window_start):
                probe["timeout_seen"] = True
                break
            if time.monotonic() >= deadline:
                probe["safety_deadline_hit"] = True
                break

            round_no = int(probe["probe_rounds"]) + 1
            probe["probe_rounds"] = round_no
            round_info: Dict[str, object] = {"round": round_no}

            wake_start = self.play(f"CYCLE_{cycle}_PROBE_ROUND_{round_no}_WAKE_AGAIN", "wake")
            remaining = max(0.0, deadline - time.monotonic())
            self.wait(min(self.args.wake_retry_gap, remaining))
            wake_count = self.count_since(wake_start, WAKE_RE)
            round_info.update(
                {
                    "wake_analysis_start_index": wake_start,
                    "wake_detected_count": wake_count,
                }
            )

            open_count = 0
            close_count = 0
            if not self.timeout_seen_since(window_start) and time.monotonic() < deadline:
                open_start = self.play(f"CYCLE_{cycle}_PROBE_ROUND_{round_no}_OPEN", "open")
                remaining = max(0.0, deadline - time.monotonic())
                self.wait(min(1.2, remaining))
                open_count = self.count_since(open_start, OPEN_RE)
                round_info.update(
                    {
                        "open_analysis_start_index": open_start,
                        "open_recognized_count": open_count,
                    }
                )

            if not self.timeout_seen_since(window_start) and time.monotonic() < deadline:
                close_start = self.play(f"CYCLE_{cycle}_PROBE_ROUND_{round_no}_CLOSE", "close")
                remaining = max(0.0, deadline - time.monotonic())
                self.wait(min(1.2, remaining))
                close_count = self.count_since(close_start, CLOSE_RE)
                round_info.update(
                    {
                        "close_analysis_start_index": close_start,
                        "close_recognized_count": close_count,
                    }
                )

            probe["wake_detected_count"] = int(probe["wake_detected_count"]) + wake_count
            probe["open_recognized_count"] = int(probe["open_recognized_count"]) + open_count
            probe["close_recognized_count"] = int(probe["close_recognized_count"]) + close_count
            command_count = int(probe["open_recognized_count"]) + int(probe["close_recognized_count"])
            probe["command_recognized_count"] = command_count

            round_full_pass = wake_count > 0 and open_count > 0 and close_count > 0
            round_activity = wake_count > 0 or open_count > 0 or close_count > 0
            round_info.update(
                {
                    "round_activity": round_activity,
                    "round_full_pass": round_full_pass,
                    "analysis_end_index": len(self.records),
                }
            )
            probe["rounds"].append(round_info)
            self.mark(f"CYCLE_{cycle}_PROBE_ROUND_{round_no}_ANALYSIS", json.dumps(round_info, ensure_ascii=False))

            if round_activity:
                probe["any_activity_before_timeout"] = True
            if round_full_pass:
                probe["full_pass_before_timeout"] = True
                break
            if self.timeout_seen_since(window_start):
                probe["timeout_seen"] = True
                break

        if int(probe["wake_detected_count"]) == 0 and int(probe["command_recognized_count"]) == 0:
            probe["target_failure_candidate"] = True

        if probe["timeout_seen"] and probe["target_failure_candidate"]:
            recovery_wake_start = self.play(f"CYCLE_{cycle}_RECOVERY_WAKE_AFTER_TARGET_TIMEOUT", "wake")
            ok_mode, _ = self.wait_for(
                f"CYCLE_{cycle}_RECOVERY_MODE1_AFTER_TARGET_TIMEOUT",
                MODE1_RE,
                8,
                start=recovery_wake_start,
            )
            probe["recovery_mode1_after_timeout"] = ok_mode
            if ok_mode:
                recovery_open_start = self.play(f"CYCLE_{cycle}_RECOVERY_OPEN_AFTER_TARGET_TIMEOUT", "open")
                self.wait(1.4)
                probe["recovery_open_recognized_after_timeout"] = self.count_since(recovery_open_start, OPEN_RE) > 0
                recovery_close_start = self.play(f"CYCLE_{cycle}_RECOVERY_CLOSE_AFTER_TARGET_TIMEOUT", "close")
                self.wait(1.4)
                probe["recovery_close_recognized_after_timeout"] = self.count_since(recovery_close_start, CLOSE_RE) > 0

        recovery_ok = (
            probe["recovery_mode1_after_timeout"] is True
            and probe["recovery_open_recognized_after_timeout"] is True
            and probe["recovery_close_recognized_after_timeout"] is True
        )
        if probe["full_pass_before_timeout"] and int(probe["probe_rounds"]) == 1:
            probe["classification"] = "pass_first_window_wake_again_and_commands_recognized"
        elif probe["full_pass_before_timeout"]:
            probe["classification"] = "suspect_first_window_initial_miss_recovered_before_timeout"
        elif probe["target_failure_candidate"] and probe["timeout_seen"] and recovery_ok:
            probe["classification"] = "fail_first_window_no_wake_no_command_until_timeout_recovered"
        elif probe["target_failure_candidate"] and probe["timeout_seen"]:
            probe["classification"] = "fail_first_window_no_activity_and_recovery_failed"
        elif probe["any_activity_before_timeout"]:
            probe["classification"] = "suspect_first_window_activity_but_not_full_pass"
        else:
            probe["classification"] = "blocked_no_timeout_seen_no_activity"
        self.mark(f"CYCLE_{cycle}_FIRST_WINDOW_UNIFIED_PROBE_END", json.dumps(probe, ensure_ascii=False))
        return probe

    def noise_probe_until_timeout(self, cycle: int, missed_name: str, window_start: int) -> Dict[str, object]:
        probe = {
            "missed_command": missed_name,
            "retry_until_timeout": self.args.retry_until_timeout,
            "wake_retry_attempts": 0,
            "wake_retry_detected_count": 0,
            "timeout_seen": False,
            "safety_deadline_hit": False,
            "recovery_mode1_after_timeout": None,
            "recovery_command_recognized": None,
            "command_retry_attempts": 0,
            "command_retry_recognized": False,
            "classification": "unknown",
        }
        self.mark(
            f"CYCLE_{cycle}_NOISE_PROBE_BEGIN",
            f"missed={missed_name} retry_until_timeout={self.args.retry_until_timeout}",
        )
        timeout_seen = False
        command_retry_recognized = False
        if self.args.retry_until_timeout:
            deadline = time.monotonic() + self.args.timeout_wait
            attempt = 0
            while True:
                self.drain()
                if any(TIMEOUT_RE.search(line) for line in self.records[window_start:]):
                    timeout_seen = True
                    break
                if time.monotonic() >= deadline:
                    probe["safety_deadline_hit"] = True
                    break
                attempt += 1
                probe["wake_retry_attempts"] = attempt
                start = self.play(f"CYCLE_{cycle}_WAKE_RETRY_{attempt}_AFTER_{missed_name.upper()}_MISS", "wake")
                remaining = max(0.0, deadline - time.monotonic())
                self.wait(min(self.args.wake_retry_gap, remaining))
                detected = self.count_since(start, WAKE_RE)
                self.mark(
                    f"CYCLE_{cycle}_WAKE_RETRY_{attempt}_ANALYSIS",
                    json.dumps(
                        {
                            "missed_command": missed_name,
                            "detected_count": detected,
                            "analysis_start_index": start,
                            "analysis_end_index": len(self.records),
                            "retry_until_timeout": True,
                        },
                        ensure_ascii=False,
                    ),
                )
                if detected > 0:
                    probe["wake_retry_detected_count"] = int(probe["wake_retry_detected_count"]) + detected
                    cmd_attempts = int(probe["command_retry_attempts"]) + 1
                    probe["command_retry_attempts"] = cmd_attempts
                    cmd_start = self.play(
                        f"CYCLE_{cycle}_COMMAND_RETRY_{cmd_attempts}_AFTER_WAKE_RETRY_{attempt}",
                        missed_name,
                    )
                    self.wait(1.4)
                    cmd_ok = self.count_since(cmd_start, self.command_regex(missed_name)) > 0
                    self.mark(
                        f"CYCLE_{cycle}_COMMAND_RETRY_{cmd_attempts}_ANALYSIS",
                        json.dumps(
                            {
                                "missed_command": missed_name,
                                "command_recognized": cmd_ok,
                                "analysis_start_index": cmd_start,
                                "analysis_end_index": len(self.records),
                                "retry_until_timeout": True,
                            },
                            ensure_ascii=False,
                        ),
                    )
                    if cmd_ok:
                        probe["command_retry_recognized"] = True
                        command_retry_recognized = True
                        break
                else:
                    self.mark(
                        f"CYCLE_{cycle}_WAKE_RETRY_{attempt}_FAIL_CONTINUING",
                        "wake not detected, continuing retry",
                    )
                if any(TIMEOUT_RE.search(line) for line in self.records[start:]):
                    timeout_seen = True
                    break
        else:
            for attempt in range(1, self.args.max_wake_retries + 1):
                self.drain()
                if any(TIMEOUT_RE.search(line) for line in self.records[window_start:]):
                    timeout_seen = True
                    break
                probe["wake_retry_attempts"] = attempt
                start = self.play(f"CYCLE_{cycle}_WAKE_RETRY_{attempt}_AFTER_{missed_name.upper()}_MISS", "wake")
                self.wait(self.args.wake_retry_gap)
                detected = self.count_since(start, WAKE_RE)
                self.mark(
                    f"CYCLE_{cycle}_WAKE_RETRY_{attempt}_ANALYSIS",
                    json.dumps(
                        {
                            "missed_command": missed_name,
                            "detected_count": detected,
                            "analysis_start_index": start,
                            "analysis_end_index": len(self.records),
                            "retry_until_timeout": False,
                        },
                        ensure_ascii=False,
                    ),
                )
                if detected > 0:
                    probe["wake_retry_detected_count"] = int(probe["wake_retry_detected_count"]) + detected
                    cmd_attempts = int(probe["command_retry_attempts"]) + 1
                    probe["command_retry_attempts"] = cmd_attempts
                    cmd_start = self.play(
                        f"CYCLE_{cycle}_COMMAND_RETRY_{cmd_attempts}_AFTER_WAKE_RETRY_{attempt}",
                        missed_name,
                    )
                    self.wait(1.4)
                    cmd_ok = self.count_since(cmd_start, self.command_regex(missed_name)) > 0
                    self.mark(
                        f"CYCLE_{cycle}_COMMAND_RETRY_{cmd_attempts}_ANALYSIS",
                        json.dumps(
                            {
                                "missed_command": missed_name,
                                "command_recognized": cmd_ok,
                                "analysis_start_index": cmd_start,
                                "analysis_end_index": len(self.records),
                                "retry_until_timeout": False,
                            },
                            ensure_ascii=False,
                        ),
                    )
                    if cmd_ok:
                        probe["command_retry_recognized"] = True
                        command_retry_recognized = True
                        break
                else:
                    self.mark(
                        f"CYCLE_{cycle}_WAKE_RETRY_{attempt}_FAIL_CONTINUING",
                        "wake not detected, continuing retry",
                    )
                if any(TIMEOUT_RE.search(line) for line in self.records[start:]):
                    timeout_seen = True
                    break
        if not timeout_seen and not self.args.retry_until_timeout:
            timeout_seen, _ = self.wait_for(
                f"CYCLE_{cycle}_TIMEOUT_AFTER_{missed_name.upper()}_MISS",
                TIMEOUT_RE,
                self.args.timeout_wait,
                start=window_start,
            )
        probe["timeout_seen"] = timeout_seen
        if timeout_seen:
            recovery_wake_start = self.play(f"CYCLE_{cycle}_RECOVERY_WAKE_AFTER_TIMEOUT", "wake")
            ok_mode, _ = self.wait_for(
                f"CYCLE_{cycle}_RECOVERY_MODE1_AFTER_TIMEOUT",
                MODE1_RE,
                8,
                start=recovery_wake_start,
            )
            probe["recovery_mode1_after_timeout"] = ok_mode
            if ok_mode:
                recovery_cmd_start = self.play(f"CYCLE_{cycle}_RECOVERY_{missed_name.upper()}_AFTER_TIMEOUT", missed_name)
                self.wait(1.4)
                probe["recovery_command_recognized"] = self.count_since(recovery_cmd_start, self.command_regex(missed_name)) > 0
        if command_retry_recognized:
            probe["classification"] = "command_recognized_after_wake_retry"
        elif probe["recovery_mode1_after_timeout"] is False or probe["recovery_command_recognized"] is False:
            probe["classification"] = "fail_recovery_after_timeout"
        elif int(probe["wake_retry_detected_count"]) > 0:
            probe["classification"] = "command_miss_despite_wake_retry"
        elif timeout_seen:
            probe["classification"] = "fail_first_window_requires_timeout_recovery"
        else:
            probe["classification"] = "blocked_no_timeout_seen"
        self.mark(f"CYCLE_{cycle}_NOISE_PROBE_END", json.dumps(probe, ensure_ascii=False))
        return probe

    def save_summary(self) -> None:
        self.summary_path.write_text(json.dumps(self.summary, ensure_ascii=False, indent=2), encoding="utf-8")

    def run_cycle(self, ser: serial.Serial, cycle: int) -> Dict[str, object]:
        cyc: Dict[str, object] = {"cycle": cycle, "status": "unknown", "noise_probe": None}
        self.mark(f"CYCLE_{cycle}_REBOOT_SEND", "reboot")
        ser.write(b"reboot\r\n")
        ser.flush()
        self.wait(5)
        boot_start = len(self.records)
        boot_ok, _ = self.wait_for(f"CYCLE_{cycle}_BOOT_READY", BOOT_RE, 35, start=boot_start)
        cyc["boot_ready"] = boot_ok
        if not boot_ok:
            cyc["status"] = "blocked_boot_not_ready"
            return cyc
        self.wait(2)
        wake_start = self.play(f"CYCLE_{cycle}_INITIAL_WAKE", "wake")
        mode_ok, _ = self.wait_for(f"CYCLE_{cycle}_MODE1_AFTER_INITIAL_WAKE", MODE1_RE, 8, start=wake_start)
        cyc["initial_wake_mode1"] = mode_ok
        cyc["initial_wake_marker_count"] = self.count_since(wake_start, WAKE_RE)
        self.mark(
            f"CYCLE_{cycle}_INITIAL_WAKE_ANALYSIS",
            json.dumps(
                {
                    "mode1": mode_ok,
                    "wake_marker_count": cyc["initial_wake_marker_count"],
                    "analysis_start_index": wake_start,
                    "analysis_end_index": len(self.records),
                },
                ensure_ascii=False,
            ),
        )
        if not mode_ok:
            cyc["status"] = "fail_initial_wake_no_mode1"
            return cyc
        window_start = len(self.records)
        self.mark(
            f"CYCLE_{cycle}_FIRST_WINDOW_BEGIN",
            "unified wake/open/close probe for target no-wake-no-command issue",
        )
        probe = self.first_window_unified_probe(cycle, window_start)
        cyc["first_window_probe"] = probe
        cyc["wake_again_marker_count"] = int(probe.get("wake_detected_count", 0))
        cyc["open_recognized_count"] = int(probe.get("open_recognized_count", 0))
        cyc["close_recognized_count"] = int(probe.get("close_recognized_count", 0))
        cyc["target_failure_candidate"] = bool(probe.get("target_failure_candidate"))
        cyc["timeout_seen_during_probe"] = bool(probe.get("timeout_seen"))
        classification = str(probe.get("classification", "unknown"))
        cyc["status"] = classification

        if cycle == self.args.cycles and self.args.wait_final_timeout and not bool(probe.get("timeout_seen")):
            ok_timeout, _ = self.wait_for(
                f"CYCLE_{cycle}_FINAL_TIMEOUT",
                TIMEOUT_RE,
                self.args.timeout_wait,
                start=window_start,
            )
            cyc["final_timeout_seen"] = ok_timeout

        if classification == "pass_first_window_wake_again_and_commands_recognized":
            self.mark(f"CYCLE_{cycle}_PASS", json.dumps(cyc, ensure_ascii=False))
        elif classification.startswith("fail_"):
            cyc["noise_probe"] = probe
            self.mark(f"CYCLE_{cycle}_FAIL", json.dumps(cyc, ensure_ascii=False))
        elif classification.startswith("blocked_"):
            cyc["noise_probe"] = probe
            self.mark(f"CYCLE_{cycle}_BLOCKED", json.dumps(cyc, ensure_ascii=False))
        else:
            cyc["noise_probe"] = probe
            self.mark(f"CYCLE_{cycle}_SUSPECT", json.dumps(cyc, ensure_ascii=False))
        return cyc

    def run(self) -> None:
        with RealtimeTaskLog(self.serial_log_path, fsync=not self.args.no_fsync, encoding="utf-8-sig") as serial_log, RealtimeTaskLog(
            self.tool_log_path, fsync=not self.args.no_fsync
        ) as tool_log:
            self.serial_log = serial_log
            self.tool_log = tool_log
            serial_log.write(f"[BEGIN] {xshell_ts_seconds(self.start_dt)}\r\n")
            serial_log.write(xshell_line(f"Connecting to {self.args.port}..."))
            tool_log.write(
                ts_line(
                    f"TOOL_START config={self.args.config} port={self.args.port} baud={self.args.baud} "
                    f"cycles={self.args.cycles} volume={self.args.volume} "
                    f"end_at={self.args.end_at_dt.isoformat(timespec='seconds') if self.args.end_at_dt else None} "
                    f"retry_until_timeout={self.args.retry_until_timeout} timeout_wait={self.args.timeout_wait} "
                    f"wake_retry_gap={self.args.wake_retry_gap} "
                    "default_local_soundcard_only xshell_serial_log_and_tool_log"
                )
            )
            self.mark(
                "CONFIG_EFFECTIVE",
                json.dumps(
                    {
                        "config": self.args.config,
                        "port": self.args.port,
                        "baud": self.args.baud,
                        "audio_dir": self.args.audio_dir,
                        "wake_file": self.args.wake_file,
                        "open_file": self.args.open_file,
                        "close_file": self.args.close_file,
                        "out_dir": self.args.out_dir,
                        "end_at": self.args.end_at_dt.isoformat(timespec="seconds") if self.args.end_at_dt else None,
                        "volume": None if self.args.skip_volume else self.args.volume,
                        "retry_until_timeout": self.args.retry_until_timeout,
                        "wake_retry_gap_seconds": self.args.wake_retry_gap,
                        "timeout_wait_seconds": self.args.timeout_wait,
                        "max_wake_retries_on_command_miss": self.args.max_wake_retries,
                        "fsync": not self.args.no_fsync,
                    },
                    ensure_ascii=False,
                ),
            )
            self.mark("PREPARE_BEGIN", "set volume and normalize audio")
            self.prepare()
            self.mark("PREPARE_DONE", json.dumps(self.summary["files"], ensure_ascii=False))
            self.mark("SERIAL_OPEN_BEGIN", f"{self.args.port}@{self.args.baud}")
            with serial.Serial(self.args.port, baudrate=self.args.baud, timeout=0.025, write_timeout=2) as ser:
                self.mark("SERIAL_OPEN_DONE", f"{self.args.port}@{self.args.baud}")
                serial_log.write(xshell_line("Connected."))
                serial_log.write(xshell_line(""))
                thread = threading.Thread(target=self.serial_reader, args=(ser,), daemon=True)
                thread.start()
                try:
                    cycle = 1
                    while cycle <= self.args.cycles:
                        if self.args.end_at_dt and now_dt() >= self.args.end_at_dt:
                            self.summary["stop_reason"] = "end_at_reached_before_next_cycle"
                            self.mark("END_AT_REACHED_BEFORE_CYCLE", self.args.end_at_dt.isoformat(timespec="seconds"))
                            break
                        cycle_result = self.run_cycle(ser, cycle)
                        self.summary["cycles"].append(cycle_result)
                        self.save_summary()
                        cycle += 1
                    else:
                        self.summary["stop_reason"] = "cycles_completed"
                    if "stop_reason" not in self.summary and self.args.end_at_dt and now_dt() >= self.args.end_at_dt:
                        self.summary["stop_reason"] = "end_at_reached_after_cycle"
                    self.summary["completed_cycles"] = len(self.summary["cycles"])
                    self.save_summary()
                finally:
                    self.mark("NOISE_AWARE_PRESSURE_DONE", json.dumps(self.summary["cycles"], ensure_ascii=False))
                    self.stop.set()
                    thread.join(timeout=1)
                    self.drain()
                    serial_log.write(xshell_line("Connection closed."))
                    serial_log.write("\r\n")
                    serial_log.write(f"[END] {xshell_ts_seconds()}\r\n")
                    tool_log.write(ts_line("TOOL_END"))
        clean_log = make_clean_log(self.serial_log_path)
        self.summary["files"]["clean_serial_log"] = str(clean_log)
        self.save_summary()
        if pygame.mixer.get_init():
            pygame.mixer.quit()
        print(str(self.serial_log_path))
        print(str(self.tool_log_path))
        print(str(clean_log))
        print(str(self.summary_path))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run noise-aware reboot first-window voice pressure test.")
    parser.add_argument("--config", default="config/arcs_config.json")
    parser.add_argument("--cycles", type=int, default=20)
    parser.add_argument("--end-at", help="Local end time, for example 2026-06-09T08:30:00. Stops before starting the next cycle after this time.")
    parser.add_argument("--port", help="Temporary override for config serial.port")
    parser.add_argument("--baud", type=int, help="Temporary override for config serial.baud")
    parser.add_argument("--audio-dir")
    parser.add_argument("--wake-file")
    parser.add_argument("--open-file")
    parser.add_argument("--close-file")
    parser.add_argument("--out-dir")
    parser.add_argument("--volume", type=int)
    parser.add_argument("--skip-volume", action="store_true", help="Do not adjust Windows default render volume.")
    parser.add_argument("--max-wake-retries", type=int)
    parser.add_argument("--retry-until-timeout", dest="retry_until_timeout", action="store_true", help="Probe wake word until first-window timeout on miss.")
    parser.add_argument("--fixed-wake-retries", dest="retry_until_timeout", action="store_false", help="Use max_wake_retries instead of probing until timeout.")
    parser.add_argument("--wake-retry-gap", type=float)
    parser.add_argument("--timeout-wait", type=float)
    parser.add_argument("--no-fsync", action="store_true", help="Flush without os.fsync; default is realtime flush+fsync.")
    parser.add_argument("--no-final-timeout", dest="wait_final_timeout", action="store_false")
    parser.set_defaults(wait_final_timeout=True, retry_until_timeout=None)
    return parser


def main() -> int:
    args = apply_config(build_parser().parse_args())
    if args.cycles < 1:
        raise RuntimeError("--cycles must be >= 1")
    if args.max_wake_retries < 1:
        raise RuntimeError("--max-wake-retries must be >= 1")
    Runner(args).run()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:  # pylint: disable=broad-except
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
