# -*- coding: utf-8 -*-
"""Capture serial lines in Xshell-compatible format plus a separate tool log."""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import time
from pathlib import Path

import serial

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent


def now_dt() -> _dt.datetime:
    return _dt.datetime.now()


def tool_ts() -> str:
    return now_dt().isoformat(timespec="milliseconds")


def xshell_ts(dt: _dt.datetime | None = None) -> str:
    dt = dt or now_dt()
    return f"{dt.year}/{dt.month}/{dt.day} {dt:%H:%M:%S}.{dt.microsecond // 1000:03d}"


def xshell_ts_seconds(dt: _dt.datetime | None = None) -> str:
    dt = dt or now_dt()
    return f"{dt.year}/{dt.month}/{dt.day} {dt:%H:%M:%S}"


def write_live(handle, text: str, fsync: bool) -> None:
    handle.write(text)
    handle.flush()
    if fsync:
        os.fsync(handle.fileno())


def tool_line(text: str) -> str:
    return f"[{tool_ts()}] {text}\n"


def xshell_line(text: str = "") -> str:
    return f"[{xshell_ts()}] {text}\r\n"


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


class TerminalLineParser:
    """Small terminal-style parser matching Xshell-readable line logging."""

    def __init__(self) -> None:
        self.current = ""
        self.ansi_tail = ""

    def feed(self, data: bytes):
        text = data.decode("utf-8", errors="replace")
        text = self.ansi_tail + text
        # If an ESC sequence is split at the end of a chunk, keep it for next time.
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
                    # CRLF is a normal serial newline; let LF emit the line.
                    pass
                else:
                    # Bare CR is terminal carriage-return behavior.
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


def port_label(port: str) -> str:
    digits = "".join(ch for ch in port if ch.isdigit())
    if digits:
        return f"COM{int(digits):03d}"
    return port


def main() -> int:
    parser = argparse.ArgumentParser(description="Debug Xshell-format serial log plus separated tool log.")
    parser.add_argument("--config", default=str(SKILL_DIR / "config" / "arcs_config.json"))
    parser.add_argument("--port")
    parser.add_argument("--baud", type=int)
    parser.add_argument("--lines", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--out-dir")
    parser.add_argument("--no-fsync", action="store_true")
    args = parser.parse_args()

    config = load_config(args.config)
    port = args.port or cfg_get(config, "serial.port", "COM28")
    baud = args.baud or int(cfg_get(config, "serial.baud", 921600))
    out_root = args.out_dir or cfg_get(config, "logging.out_dir", "result")
    fsync = not args.no_fsync and bool(cfg_get(config, "logging.fsync", True))

    start_dt = now_dt()
    task_stamp = start_dt.strftime("%Y%m%d_%H%M%S")
    xshell_stamp = start_dt.strftime("%Y-%m-%d_%H_%M_%S")
    out_dir = Path(out_root) / task_stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    serial_file = out_dir / f"{port_label(port)}_{baud}_{xshell_stamp}.log"
    tool_file = out_dir / f"serial_realtime_save_{args.lines}lines.tool.log"

    serial_lines = 0
    serial_chunks = 0
    serial_bytes = 0
    parser_state = TerminalLineParser()
    start = time.time()

    with serial.Serial(port, baudrate=baud, timeout=0.1) as ser, serial_file.open(
        "w", encoding="utf-8-sig", errors="replace", newline=""
    ) as serial_out, tool_file.open("w", encoding="utf-8", errors="replace", newline="") as tool_out:
        write_live(serial_out, f"[BEGIN] {xshell_ts_seconds(start_dt)}\r\n", fsync)
        write_live(serial_out, xshell_line(f"Connecting to {port}..."), fsync)
        write_live(serial_out, xshell_line("Connected."), fsync)
        write_live(serial_out, xshell_line(""), fsync)
        write_live(
            tool_out,
            tool_line(
                f"TOOL_START config={args.config} port={port} baud={baud} target_xshell_serial_lines={args.lines} "
                f"serial_log={serial_file} fsync={fsync}"
            ),
            fsync,
        )
        while serial_lines < args.lines and time.time() - start < args.timeout:
            data = ser.read(4096)
            if not data:
                continue
            serial_chunks += 1
            serial_bytes += len(data)
            lines = parser_state.feed(data)
            for line in lines:
                if serial_lines >= args.lines:
                    break
                serial_lines += 1
                write_live(serial_out, xshell_line(line.replace("\x1b", "")), fsync)
            write_live(
                tool_out,
                tool_line(
                    f"SERIAL_READ chunk={serial_chunks} bytes={len(data)} total_bytes={serial_bytes} "
                    f"emitted_lines={len(lines)} saved_lines={serial_lines}"
                ),
                fsync,
            )
        write_live(serial_out, xshell_line("Connection closed."), fsync)
        write_live(serial_out, "\r\n", fsync)
        write_live(serial_out, f"[END] {xshell_ts_seconds()}\r\n", fsync)
        write_live(
            tool_out,
            tool_line(
                f"TOOL_END serial_lines={serial_lines} serial_chunks={serial_chunks} "
                f"serial_bytes={serial_bytes} elapsed_s={time.time() - start:.3f}"
            ),
            fsync,
        )

    print(serial_file)
    print(tool_file)
    print(f"serial_lines={serial_lines}")
    print(f"serial_chunks={serial_chunks}")
    print(f"serial_bytes={serial_bytes}")
    return 0 if serial_lines == args.lines else 2


if __name__ == "__main__":
    raise SystemExit(main())
