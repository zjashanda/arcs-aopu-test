import argparse
import csv
import json
import re
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


ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
TIMEOUT_RE = re.compile(
    r"wakeup timeout|wakeup_algo_timeout_cb|wakeup switch mode to:\s*0|"
    r"wakeup_algo_switch_mode_cb:\s*mode=0|\bMODE=0\b",
    re.I,
)


THRESHOLDS = [
    {"level": 1, "name": "low", "label": "低阈值", "hex": "A5 08 02 00 EE 01 00 9E"},
    {"level": 2, "name": "middle", "label": "中阈值", "hex": "A5 08 02 00 EE 02 00 9F"},
    {"level": 3, "name": "high_inferred", "label": "第三档推测阈值", "hex": "A5 08 02 00 EE 03 00 A0"},
]


def clean_line(line):
    return ANSI_RE.sub("", line).replace("[ap]", "")


def parse_hex(value):
    return bytes.fromhex(value.replace(" ", "").replace(",", ""))


def read_protocol_response(port, seconds):
    deadline = time.monotonic() + seconds
    chunks = []
    while time.monotonic() < deadline:
        data = port.read(256)
        if data:
            chunks.append(data)
        else:
            time.sleep(0.03)
    return b"".join(chunks)


def wait_for_timeout(collector, start_mono, wait_s):
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        lines = [clean_line(x["line"]) for x in collector.snapshot(start_mono, time.monotonic())]
        if any(TIMEOUT_RE.search(line) for line in lines):
            return True
        time.sleep(0.25)
    return False


def extract_algo_values(items):
    values = defaultdict(list)
    cleaned = [clean_line(item["line"]) for item in items]
    for line in cleaned:
        for key, val in re.findall(r'"key"\s*:\s*"?(.*?)"?\s*,\s*"val"\s*:\s*(-?\d+)', line):
            values[key].append(int(val))
        for key in ("ncmThreshold", "nStartStateThreshold", "ncm"):
            for val in re.findall(rf'"{key}"\s*:\s*(-?\d+)', line):
                values[key].append(int(val))
    return {key: sorted(set(vals)) for key, vals in values.items()}


def summarize_window(collector, start_mono, end_mono, expected=None):
    items = collector.snapshot(start_mono, end_mono + 0.2)
    matches = extract_matches(items)
    if expected is None:
        status = "PASS" if matches["wake"] else "FAIL_NO_WAKE"
    else:
        recognized = " | ".join(matches["recognized"])
        status = "PASS" if expected in recognized else "FAIL_NO_COMMAND"
    return {
        "status": status,
        "serial_lines": len(items),
        "wake_hits": len(matches["wake"]),
        "mode1_hits": len(matches["mode1"]),
        "mode0_hits": len(matches["mode0"]),
        "recognized_hits": len(matches["recognized"]),
        "submitted_hits": len(matches["submitted"]),
        "recognized_text": " | ".join(matches["recognized"]),
        "submitted_text": " | ".join(matches["submitted"]),
        "algo_values": extract_algo_values(items),
    }


def play_and_summarize(pygame, collector, audio_path, post_gap, expected=None):
    start = time.monotonic()
    play_audio(pygame, audio_path)
    time.sleep(post_gap)
    end = time.monotonic()
    return summarize_window(collector, start, end, expected)


def compact_values(values):
    keys = ["1414", "1410", "2121", "2122", "24583", "ncmThreshold", "nStartStateThreshold", "ncm"]
    return {key: values.get(key, []) for key in keys if key in values}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--protocol-port", default="COM28")
    parser.add_argument("--protocol-baud", type=int, default=9600)
    parser.add_argument("--log-port", default="COM26")
    parser.add_argument("--log-baud", type=int, default=921600)
    parser.add_argument("--audio-dir", default=str(SKILL_DIR / "assets" / "MP3"))
    parser.add_argument("--out-dir", default=str(SKILL_DIR / "result_threshold_timeout_algorithm"))
    parser.add_argument("--protocol-read-seconds", type=float, default=1.5)
    parser.add_argument("--post-gap", type=float, default=2.0)
    parser.add_argument("--timeout-wait", type=float, default=22.0)
    parser.add_argument("--gap", type=float, default=1.0)
    args = parser.parse_args()

    root = Path.cwd()
    audio_dir = Path(args.audio_dir)
    if not audio_dir.is_absolute():
        audio_dir = root / audio_dir
    out_root = Path(args.out_dir)
    if not out_root.is_absolute():
        out_root = root / out_root

    wake_audio = audio_dir / "你好小奥.mp3"
    open_audio = audio_dir / "打开浴霸.mp3"
    close_audio = audio_dir / "关闭浴霸.mp3"
    for audio in (wake_audio, open_audio, close_audio):
        if not audio.exists():
            raise FileNotFoundError(audio)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = out_root / timestamp
    out_dir.mkdir(parents=True, exist_ok=True)
    serial_log = out_dir / f"{args.log_port}_{args.log_baud}_{datetime.now().strftime('%Y-%m-%d_%H_%M_%S')}.log"
    csv_path = out_dir / "threshold_timeout_algorithm_probe.csv"
    summary_path = out_dir / "threshold_timeout_algorithm_probe.summary.json"

    import serial

    rows = []
    started_at = datetime.now()
    pygame = setup_pygame()
    collector = SerialCollector(args.log_port, args.log_baud, serial_log)
    protocol = serial.Serial(args.protocol_port, args.protocol_baud, timeout=0.1)
    try:
        collector.start()
        time.sleep(1.0)
        for cycle in range(1, args.cycles + 1):
            for threshold in THRESHOLDS:
                phase_start = time.monotonic()
                protocol.reset_input_buffer()
                tx = parse_hex(threshold["hex"])
                protocol.write(tx)
                protocol.flush()
                rx = read_protocol_response(protocol, args.protocol_read_seconds)
                time.sleep(args.gap)

                prewake = play_and_summarize(pygame, collector, wake_audio, args.post_gap)
                timeout_start = time.monotonic()
                timeout_seen = wait_for_timeout(collector, timeout_start, args.timeout_wait)
                timeout_window = summarize_window(collector, timeout_start, time.monotonic())

                postwake = play_and_summarize(pygame, collector, wake_audio, args.post_gap)
                time.sleep(args.gap)
                open_cmd = play_and_summarize(pygame, collector, open_audio, args.post_gap, "kai yu ba")
                time.sleep(args.gap)
                wake_again = play_and_summarize(pygame, collector, wake_audio, args.post_gap)
                time.sleep(args.gap)
                close_cmd = play_and_summarize(pygame, collector, close_audio, args.post_gap, "guan yu ba")
                phase_end = time.monotonic()

                phase_values = extract_algo_values(collector.snapshot(phase_start, phase_end))
                row = {
                    "cycle": cycle,
                    "level": threshold["level"],
                    "name": threshold["name"],
                    "label": threshold["label"],
                    "tx_hex": threshold["hex"],
                    "rx_hex": rx.hex(" ").upper(),
                    "rx_len": len(rx),
                    "timeout_seen": timeout_seen,
                    "prewake_status": prewake["status"],
                    "postwake_status": postwake["status"],
                    "open_status": open_cmd["status"],
                    "wake_again_status": wake_again["status"],
                    "close_status": close_cmd["status"],
                    "mode_values": json.dumps(compact_values(phase_values), ensure_ascii=False, sort_keys=True),
                    "prewake_values": json.dumps(compact_values(prewake["algo_values"]), ensure_ascii=False, sort_keys=True),
                    "postwake_values": json.dumps(compact_values(postwake["algo_values"]), ensure_ascii=False, sort_keys=True),
                    "open_values": json.dumps(compact_values(open_cmd["algo_values"]), ensure_ascii=False, sort_keys=True),
                    "wake_again_values": json.dumps(compact_values(wake_again["algo_values"]), ensure_ascii=False, sort_keys=True),
                    "close_values": json.dumps(compact_values(close_cmd["algo_values"]), ensure_ascii=False, sort_keys=True),
                }
                rows.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
    finally:
        protocol.close()
        collector.close()
        pygame.mixer.quit()

    fields = list(rows[0].keys()) if rows else []
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    aggregate = {}
    for level in sorted({row["level"] for row in rows}):
        level_rows = [row for row in rows if row["level"] == level]
        vals = defaultdict(set)
        status_counts = Counter()
        timeout_count = 0
        for row in level_rows:
            if row["timeout_seen"]:
                timeout_count += 1
            for key in ("prewake_status", "postwake_status", "open_status", "wake_again_status", "close_status"):
                status_counts[f"{key}:{row[key]}"] += 1
            mode_values = json.loads(row["mode_values"])
            for key, items in mode_values.items():
                for item in items:
                    vals[key].add(item)
        aggregate[str(level)] = {
            "count": len(level_rows),
            "timeout_seen": timeout_count,
            "status_counts": dict(status_counts),
            "algo_values": {key: sorted(items) for key, items in vals.items()},
        }

    summary = {
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "cycles": args.cycles,
        "protocol_port": args.protocol_port,
        "protocol_baud": args.protocol_baud,
        "log_port": args.log_port,
        "log_baud": args.log_baud,
        "out_dir": str(out_dir),
        "serial_log": str(serial_log),
        "csv": str(csv_path),
        "aggregate": aggregate,
        "rows": rows,
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
