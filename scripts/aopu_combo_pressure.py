# -*- coding: utf-8 -*-
"""奥普语音/重启/三档门限组合压测入口。

该脚本复用现有 COM26 算法日志、COM28 协议串口和 MP3 音频，支持单项、
两两组合、三项组合与混合压测模式。
"""

import argparse
import csv
import json
import random
import re
import signal
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from vocab_function_coverage import SerialCollector, extract_matches, play_audio, setup_pygame


THRESHOLDS = {
    1: {"name": "low", "label": "1档", "hex": "A5 08 02 00 EE 01 00 9E"},
    2: {"name": "middle", "label": "2档", "hex": "A5 08 02 00 EE 02 00 9F"},
    3: {"name": "high", "label": "3档", "hex": "A5 08 02 00 EE 03 00 A0"},
}

COMMAND_EXPECTED = {
    "打开浴霸": "kai yu ba",
    "关闭浴霸": "guan yu ba",
}

BOOT_RE = re.compile(r"Application Ready!!!", re.I)
TIMEOUT_RE = re.compile(
    r"wakeup timeout|wakeup_algo_timeout_cb|wakeup switch mode to:\s*0|"
    r"wakeup_algo_switch_mode_cb:\s*mode=0|\bMODE=0\b",
    re.I,
)


def parse_end_at(value):
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    raise argparse.ArgumentTypeError("end time must be 'YYYY-MM-DD HH:MM:SS'")


def parse_csv_ints(value):
    out = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        level = int(item)
        if level not in THRESHOLDS:
            raise argparse.ArgumentTypeError(f"unsupported threshold level: {level}")
        out.append(level)
    if not out:
        raise argparse.ArgumentTypeError("at least one threshold level is required")
    return out


def parse_csv_words(value):
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_hex(value):
    return bytes.fromhex(value.replace(" ", "").replace(",", ""))


def now_text():
    return datetime.now().strftime("%Y/%m/%d %H:%M:%S.%f")[:-3]


def resolve_audio(audio_dir, word):
    path = audio_dir / f"{word}.mp3"
    if not path.exists():
        raise FileNotFoundError(path)
    return path


def set_default_volume_if_needed(args):
    if args.skip_volume:
        return None
    ps1 = SCRIPT_DIR / "set_default_render_volume.ps1"
    if not ps1.exists():
        return {"status": "skip", "reason": f"{ps1} not found"}
    cmd = [
        "powershell",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(ps1),
        "-Volume",
        str(args.volume),
    ]
    cp = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    return {
        "status": "ok" if cp.returncode == 0 else "failed",
        "returncode": cp.returncode,
        "stdout": cp.stdout.strip(),
        "stderr": cp.stderr.strip(),
    }


def wait_for_pattern(collector, pattern, timeout_s, start_mono=None):
    start_mono = start_mono or time.monotonic()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        items = collector.snapshot(start_mono, time.monotonic())
        for item in items:
            if pattern.search(item["line"]):
                return True, item["line"]
        time.sleep(0.2)
    return False, ""


def wait_for_timeout(collector, timeout_s, start_mono=None):
    return wait_for_pattern(collector, TIMEOUT_RE, timeout_s, start_mono)


def read_protocol_response(protocol, seconds):
    deadline = time.monotonic() + seconds
    chunks = []
    while time.monotonic() < deadline:
        data = protocol.read(256)
        if data:
            chunks.append(data)
        else:
            time.sleep(0.03)
    return b"".join(chunks)


def has_protocol(mode):
    return "threshold" in mode or mode == "mixed"


def has_reboot(mode):
    return "reboot" in mode or mode == "mixed"


def command_status(matches, expected):
    recognized = " | ".join(matches["recognized"])
    submitted = " | ".join(matches["submitted"])
    combined = f"{recognized} | {submitted}"
    if expected and expected in combined:
        return "PASS"
    if not expected and (matches["recognized"] or matches["submitted"]):
        return "PASS"
    if matches["recognized"] or matches["submitted"]:
        return "WARN_OTHER_COMMAND"
    return "FAIL_NO_COMMAND"


def wake_status(matches):
    if matches["wake"] or matches["mode1"]:
        return "PASS"
    return "FAIL_NO_WAKE"


class ComboRunner:
    def __init__(self, args):
        self.args = args
        self.root = Path.cwd()
        self.audio_dir = Path(args.audio_dir)
        if not self.audio_dir.is_absolute():
            self.audio_dir = self.root / self.audio_dir
        self.out_root = Path(args.out_dir)
        if not self.out_root.is_absolute():
            self.out_root = self.root / self.out_root
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.out_dir = self.out_root / f"{timestamp}_{args.mode}"
        self.out_dir.mkdir(parents=True, exist_ok=True)

        self.wake_audio = resolve_audio(self.audio_dir, args.wake_word)
        self.command_words = parse_csv_words(args.commands)
        self.command_audios = {word: resolve_audio(self.audio_dir, word) for word in self.command_words}
        self.threshold_levels = parse_csv_ints(args.threshold_levels)

        self.serial_log = self.out_dir / f"{args.log_port}_{args.log_baud}_{datetime.now():%Y-%m-%d_%H_%M_%S}.log"
        self.csv_path = self.out_dir / "aopu_combo_pressure.csv"
        self.summary_path = self.out_dir / "aopu_combo_pressure.summary.json"
        self.plan_path = self.out_dir / "aopu_combo_pressure.plan.json"
        self.heartbeat_path = self.out_dir / "heartbeat.txt"

        self.pygame = None
        self.collector = None
        self.protocol = None
        self.rows = []
        self.status_counts = Counter()
        self.phase_counts = defaultdict(Counter)
        self.stop_requested = False
        self.global_index = 0
        self.started_at = datetime.now()
        self.volume_result = None

        self.fields = [
            "global_index",
            "cycle",
            "cycle_mode",
            "phase",
            "threshold_level",
            "threshold_label",
            "tx_hex",
            "rx_hex",
            "rx_len",
            "action",
            "word",
            "expected",
            "status",
            "boot_ready",
            "timeout_seen",
            "serial_lines",
            "wake_hits",
            "mode1_hits",
            "mode0_hits",
            "recognized_text",
            "submitted_text",
            "start_host",
            "end_host",
            "note",
        ]

    def write_plan(self):
        plan = {
            "mode": self.args.mode,
            "cycles": self.args.cycles,
            "end_at": self.args.end_at.isoformat(sep=" ", timespec="seconds") if self.args.end_at else None,
            "log_port": self.args.log_port,
            "log_baud": self.args.log_baud,
            "protocol_port": self.args.protocol_port if has_protocol(self.args.mode) else None,
            "protocol_baud": self.args.protocol_baud if has_protocol(self.args.mode) else None,
            "volume": None if self.args.skip_volume else self.args.volume,
            "wake_word": self.args.wake_word,
            "commands": self.command_words,
            "threshold_levels": self.threshold_levels,
            "threshold_timeout": self.args.threshold_timeout,
            "mixed_modes": parse_csv_words(self.args.mixed_modes),
        }
        self.plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    def open_csv(self):
        with self.csv_path.open("w", encoding="utf-8-sig", newline="") as f:
            csv.DictWriter(f, fieldnames=self.fields).writeheader()

    def append_row(self, row):
        self.global_index += 1
        row["global_index"] = self.global_index
        self.rows.append(row)
        self.status_counts[row["status"]] += 1
        self.phase_counts[row["phase"]][row["status"]] += 1
        with self.csv_path.open("a", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=self.fields)
            writer.writerow({field: row.get(field, "") for field in self.fields})
        if self.global_index % self.args.summary_every == 0:
            self.save_summary(status="running")

    def save_summary(self, status):
        data = {
            "status": status,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "mode": self.args.mode,
            "cycles_target": self.args.cycles,
            "end_at": self.args.end_at.isoformat(sep=" ", timespec="seconds") if self.args.end_at else None,
            "global_index": self.global_index,
            "status_counts": dict(self.status_counts),
            "phase_status_counts": {k: dict(v) for k, v in self.phase_counts.items()},
            "volume_result": self.volume_result,
            "out_dir": str(self.out_dir),
            "csv": str(self.csv_path),
            "serial_log": str(self.serial_log),
            "plan": str(self.plan_path),
        }
        self.summary_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.heartbeat_path.write_text(
            f"{datetime.now():%Y-%m-%d %H:%M:%S} status={status} rows={self.global_index}\n",
            encoding="utf-8",
        )

    def row_base(self, cycle, cycle_mode, phase, start, end):
        return {
            "cycle": cycle,
            "cycle_mode": cycle_mode,
            "phase": phase,
            "start_host": start,
            "end_host": end,
        }

    def send_threshold(self, cycle, cycle_mode, level):
        threshold = THRESHOLDS[level]
        start_mono = time.monotonic()
        start_host = now_text()
        rx = b""
        status = "PASS"
        note = ""
        if not self.protocol:
            status = "BLOCKED_NO_PROTOCOL_PORT"
            note = "protocol serial not opened"
        else:
            try:
                self.protocol.reset_input_buffer()
                tx = parse_hex(threshold["hex"])
                self.protocol.write(tx)
                self.protocol.flush()
                rx = read_protocol_response(self.protocol, self.args.protocol_read_seconds)
                if not rx:
                    status = "FAIL_NO_PROTOCOL_RX"
            except Exception as exc:  # pylint: disable=broad-except
                status = "FAIL_PROTOCOL_ERROR"
                note = str(exc)
        end_mono = time.monotonic()
        end_host = now_text()
        items = self.collector.snapshot(start_mono, end_mono + 0.2)
        row = self.row_base(cycle, cycle_mode, "threshold_set", start_host, end_host)
        row.update(
            {
                "threshold_level": level,
                "threshold_label": threshold["label"],
                "tx_hex": threshold["hex"],
                "rx_hex": rx.hex(" ").upper(),
                "rx_len": len(rx),
                "action": "send_threshold",
                "status": status,
                "serial_lines": len(items),
                "note": note,
            }
        )
        self.append_row(row)
        time.sleep(self.args.gap)

    def reboot(self, cycle, cycle_mode):
        start_mono = time.monotonic()
        start_host = now_text()
        status = "PASS"
        note = ""
        try:
            self.collector.serial.write(b"reboot\r\n")
            self.collector.serial.flush()
        except Exception as exc:  # pylint: disable=broad-except
            status = "FAIL_REBOOT_SEND"
            note = str(exc)
        boot_ok = False
        matched = ""
        if status == "PASS":
            boot_ok, matched = wait_for_pattern(self.collector, BOOT_RE, self.args.boot_timeout, start_mono)
            if not boot_ok:
                status = "FAIL_BOOT_NOT_READY"
        if boot_ok and self.args.post_reboot_grace > 0:
            time.sleep(self.args.post_reboot_grace)
        end_mono = time.monotonic()
        end_host = now_text()
        items = self.collector.snapshot(start_mono, end_mono + 0.2)
        row = self.row_base(cycle, cycle_mode, "reboot", start_host, end_host)
        row.update(
            {
                "action": "reboot",
                "status": status,
                "boot_ready": boot_ok,
                "serial_lines": len(items),
                "note": note or matched,
            }
        )
        self.append_row(row)
        return boot_ok

    def play_wake(self, cycle, cycle_mode, phase):
        start_mono = time.monotonic()
        start_host = now_text()
        play_audio(self.pygame, self.wake_audio)
        time.sleep(self.args.post_gap)
        end_mono = time.monotonic()
        end_host = now_text()
        items = self.collector.snapshot(start_mono, end_mono + 0.2)
        matches = extract_matches(items)
        row = self.row_base(cycle, cycle_mode, phase, start_host, end_host)
        row.update(
            {
                "action": "wake",
                "word": self.args.wake_word,
                "status": wake_status(matches),
                "serial_lines": len(items),
                "wake_hits": len(matches["wake"]),
                "mode1_hits": len(matches["mode1"]),
                "mode0_hits": len(matches["mode0"]),
                "recognized_text": " | ".join(matches["recognized"]),
                "submitted_text": " | ".join(matches["submitted"]),
            }
        )
        self.append_row(row)

    def play_command(self, cycle, cycle_mode, word, phase="command"):
        # 每条命令前补一次唤醒，保证命令窗口存在。
        self.play_wake(cycle, cycle_mode, f"{phase}_wake_before")
        time.sleep(self.args.wake_gap)
        audio = self.command_audios[word]
        expected = COMMAND_EXPECTED.get(word)
        start_mono = time.monotonic()
        start_host = now_text()
        play_audio(self.pygame, audio)
        time.sleep(self.args.post_gap)
        end_mono = time.monotonic()
        end_host = now_text()
        items = self.collector.snapshot(start_mono, end_mono + 0.2)
        matches = extract_matches(items)
        row = self.row_base(cycle, cycle_mode, phase, start_host, end_host)
        row.update(
            {
                "action": "command",
                "word": word,
                "expected": expected or "",
                "status": command_status(matches, expected),
                "serial_lines": len(items),
                "wake_hits": len(matches["wake"]),
                "mode1_hits": len(matches["mode1"]),
                "mode0_hits": len(matches["mode0"]),
                "recognized_text": " | ".join(matches["recognized"]),
                "submitted_text": " | ".join(matches["submitted"]),
            }
        )
        self.append_row(row)

    def wait_timeout_phase(self, cycle, cycle_mode):
        start_mono = time.monotonic()
        start_host = now_text()
        timeout_seen, matched = wait_for_timeout(self.collector, self.args.timeout_wait, start_mono)
        end_mono = time.monotonic()
        end_host = now_text()
        items = self.collector.snapshot(start_mono, end_mono + 0.2)
        row = self.row_base(cycle, cycle_mode, "wait_timeout", start_host, end_host)
        row.update(
            {
                "action": "wait_timeout",
                "status": "PASS" if timeout_seen else "FAIL_TIMEOUT_NOT_SEEN",
                "timeout_seen": timeout_seen,
                "serial_lines": len(items),
                "note": matched,
            }
        )
        self.append_row(row)

    def voice_sequence(self, cycle, cycle_mode, phase_prefix="voice"):
        self.play_wake(cycle, cycle_mode, f"{phase_prefix}_wake")
        for word in self.command_words:
            time.sleep(self.args.gap)
            self.play_command(cycle, cycle_mode, word, f"{phase_prefix}_command")

    def threshold_voice_sequence(self, cycle, cycle_mode):
        for level in self.threshold_levels:
            self.send_threshold(cycle, cycle_mode, level)
            self.play_wake(cycle, cycle_mode, "threshold_prewake")
            if self.args.threshold_timeout:
                self.wait_timeout_phase(cycle, cycle_mode)
                self.play_wake(cycle, cycle_mode, "threshold_post_timeout_wake")
            for word in self.command_words:
                time.sleep(self.args.gap)
                self.play_command(cycle, cycle_mode, word, "threshold_command")

    def threshold_only_sequence(self, cycle, cycle_mode):
        for level in self.threshold_levels:
            self.send_threshold(cycle, cycle_mode, level)

    def run_cycle(self, cycle, cycle_mode):
        if cycle_mode == "voice-only":
            self.voice_sequence(cycle, cycle_mode)
        elif cycle_mode == "threshold-only":
            self.threshold_only_sequence(cycle, cycle_mode)
        elif cycle_mode == "threshold-voice":
            self.threshold_voice_sequence(cycle, cycle_mode)
        elif cycle_mode == "reboot-only":
            if self.reboot(cycle, cycle_mode):
                self.play_wake(cycle, cycle_mode, "reboot_wake")
        elif cycle_mode == "reboot-voice":
            if self.reboot(cycle, cycle_mode):
                self.voice_sequence(cycle, cycle_mode, "reboot_voice")
        elif cycle_mode == "reboot-threshold":
            if self.reboot(cycle, cycle_mode):
                self.threshold_only_sequence(cycle, cycle_mode)
        elif cycle_mode == "reboot-threshold-voice":
            if self.reboot(cycle, cycle_mode):
                self.threshold_voice_sequence(cycle, cycle_mode)
        else:
            raise RuntimeError(f"unsupported cycle mode: {cycle_mode}")

    def select_cycle_mode(self):
        if self.args.mode != "mixed":
            return self.args.mode
        modes = parse_csv_words(self.args.mixed_modes)
        return random.choice(modes)

    def should_stop(self, next_cycle):
        if self.stop_requested:
            return True
        if self.args.end_at and datetime.now() >= self.args.end_at:
            return True
        return next_cycle > self.args.cycles

    def request_stop(self, signum, _frame):
        self.stop_requested = True
        print(f"stop requested by signal {signum}", flush=True)

    def run(self):
        import serial

        random.seed(self.args.random_seed)
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)
        self.write_plan()
        self.open_csv()
        self.volume_result = set_default_volume_if_needed(self.args)

        self.pygame = setup_pygame()
        self.collector = SerialCollector(self.args.log_port, self.args.log_baud, self.serial_log)
        try:
            self.collector.start()
            time.sleep(1.0)
            if has_protocol(self.args.mode):
                self.protocol = serial.Serial(self.args.protocol_port, self.args.protocol_baud, timeout=0.1)
            cycle = 1
            while not self.should_stop(cycle):
                cycle_mode = self.select_cycle_mode()
                print(f"cycle={cycle} mode={cycle_mode}", flush=True)
                self.run_cycle(cycle, cycle_mode)
                self.save_summary(status="running")
                cycle += 1
            self.save_summary(status="finished")
        finally:
            if self.protocol:
                self.protocol.close()
            if self.collector:
                self.collector.close()
            if self.pygame:
                self.pygame.mixer.quit()
        print(json.dumps({"out_dir": str(self.out_dir), "summary": str(self.summary_path)}, ensure_ascii=False, indent=2))


def build_parser():
    parser = argparse.ArgumentParser(description="奥普语音/重启/三档门限组合压测")
    parser.add_argument(
        "--mode",
        choices=[
            "voice-only",
            "reboot-only",
            "threshold-only",
            "threshold-voice",
            "reboot-voice",
            "reboot-threshold",
            "reboot-threshold-voice",
            "mixed",
        ],
        required=True,
    )
    parser.add_argument("--cycles", type=int, default=10)
    parser.add_argument("--end-at", type=parse_end_at)
    parser.add_argument("--log-port", default="COM26")
    parser.add_argument("--log-baud", type=int, default=921600)
    parser.add_argument("--protocol-port", default="COM28")
    parser.add_argument("--protocol-baud", type=int, default=9600)
    parser.add_argument("--audio-dir", default=str(SKILL_DIR / "assets" / "MP3"))
    parser.add_argument("--out-dir", default=str(SKILL_DIR / "result_combo_pressure"))
    parser.add_argument("--volume", type=int, default=20)
    parser.add_argument("--skip-volume", action="store_true")
    parser.add_argument("--wake-word", default="你好小奥")
    parser.add_argument("--commands", default="打开浴霸,关闭浴霸")
    parser.add_argument("--threshold-levels", default="1,2,3")
    parser.add_argument("--protocol-read-seconds", type=float, default=1.5)
    parser.add_argument("--post-gap", type=float, default=2.0)
    parser.add_argument("--wake-gap", type=float, default=1.2)
    parser.add_argument("--gap", type=float, default=1.0)
    parser.add_argument("--boot-timeout", type=float, default=45.0)
    parser.add_argument("--post-reboot-grace", type=float, default=3.0)
    parser.add_argument("--timeout-wait", type=float, default=22.0)
    parser.add_argument("--threshold-timeout", dest="threshold_timeout", action="store_true")
    parser.add_argument("--no-threshold-timeout", dest="threshold_timeout", action="store_false")
    parser.set_defaults(threshold_timeout=True)
    parser.add_argument(
        "--mixed-modes",
        default="voice-only,threshold-voice,reboot-voice,reboot-threshold-voice",
        help="Only used by --mode mixed.",
    )
    parser.add_argument("--random-seed", type=int, default=20260625)
    parser.add_argument("--summary-every", type=int, default=5)
    return parser


def main():
    args = build_parser().parse_args()
    if args.cycles < 1:
        raise RuntimeError("--cycles must be >= 1")
    ComboRunner(args).run()


if __name__ == "__main__":
    main()
