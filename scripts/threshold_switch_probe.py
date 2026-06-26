import argparse
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from vocab_function_coverage import SerialCollector, extract_matches, play_audio, setup_pygame


THRESHOLD_COMMANDS = [
    {
        "name": "low_threshold_fan_on",
        "label": "开风-低阈值",
        "hex": "A5 08 02 00 EE 01 00 9E",
    },
    {
        "name": "mid_threshold_fan_off",
        "label": "关风-中阈值",
        "hex": "A5 08 02 00 EE 02 00 9F",
    },
    {
        "name": "high_threshold_inferred",
        "label": "推测第三档阈值",
        "hex": "A5 08 02 00 EE 03 00 A0",
    },
]


def parse_hex(value):
    compact = value.replace(" ", "").replace(",", "")
    if len(compact) % 2:
        raise ValueError(f"invalid hex length: {value}")
    return bytes.fromhex(compact)


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


def match_status(matches, expected=None):
    if expected is None:
        return "PASS" if matches["wake"] else "FAIL_NO_WAKE"
    text = " | ".join(matches["recognized"])
    return "PASS" if expected in text else "FAIL_NO_COMMAND"


def run_audio_case(pygame, collector, audio_path, post_gap, expected=None):
    case_start = time.monotonic()
    play_audio(pygame, audio_path)
    time.sleep(post_gap)
    case_end = time.monotonic()
    lines = collector.snapshot(case_start, case_end + 0.2)
    matches = extract_matches(lines)
    return {
        "status": match_status(matches, expected),
        "serial_lines": len(lines),
        "wake_hits": len(matches["wake"]),
        "recognized_hits": len(matches["recognized"]),
        "submitted_hits": len(matches["submitted"]),
        "recognized_text": " | ".join(matches["recognized"]),
        "submitted_text": " | ".join(matches["submitted"]),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--protocol-port", default="COM28")
    parser.add_argument("--protocol-baud", type=int, default=9600)
    parser.add_argument("--log-port", default="COM26")
    parser.add_argument("--log-baud", type=int, default=921600)
    parser.add_argument("--audio-dir", default=str(SKILL_DIR / "assets" / "MP3"))
    parser.add_argument("--out-dir", default=str(SKILL_DIR / "result_threshold_switch"))
    parser.add_argument("--protocol-read-seconds", type=float, default=1.5)
    parser.add_argument("--wake-gap", type=float, default=1.2)
    parser.add_argument("--post-gap", type=float, default=2.0)
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
    csv_path = out_dir / "threshold_switch_probe.csv"
    summary_path = out_dir / "threshold_switch_probe.summary.json"

    import serial

    rows = []
    started_at = datetime.now()
    pygame = setup_pygame()
    collector = SerialCollector(args.log_port, args.log_baud, serial_log)
    protocol = serial.Serial(args.protocol_port, args.protocol_baud, timeout=0.1)
    try:
        collector.start()
        time.sleep(1.0)
        for command in THRESHOLD_COMMANDS:
            tx = parse_hex(command["hex"])
            protocol.reset_input_buffer()
            protocol.write(tx)
            protocol.flush()
            rx = read_protocol_response(protocol, args.protocol_read_seconds)
            time.sleep(0.6)

            wake_result = run_audio_case(pygame, collector, wake_audio, args.post_gap)
            time.sleep(args.wake_gap)
            open_result = run_audio_case(pygame, collector, open_audio, args.post_gap, "kai yu ba")
            time.sleep(args.wake_gap)
            play_audio(pygame, wake_audio)
            time.sleep(args.wake_gap)
            close_result = run_audio_case(pygame, collector, close_audio, args.post_gap, "guan yu ba")

            row = {
                "name": command["name"],
                "label": command["label"],
                "tx_hex": command["hex"],
                "rx_hex": rx.hex(" ").upper(),
                "rx_len": len(rx),
                "wake_status": wake_result["status"],
                "wake_hits": wake_result["wake_hits"],
                "open_status": open_result["status"],
                "open_recognized": open_result["recognized_text"],
                "close_status": close_result["status"],
                "close_recognized": close_result["recognized_text"],
            }
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    finally:
        protocol.close()
        collector.close()
        pygame.mixer.quit()

    fields = [
        "name",
        "label",
        "tx_hex",
        "rx_hex",
        "rx_len",
        "wake_status",
        "wake_hits",
        "open_status",
        "open_recognized",
        "close_status",
        "close_recognized",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    summary = {
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "protocol_port": args.protocol_port,
        "protocol_baud": args.protocol_baud,
        "log_port": args.log_port,
        "log_baud": args.log_baud,
        "out_dir": str(out_dir),
        "serial_log": str(serial_log),
        "csv": str(csv_path),
        "rows": rows,
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
