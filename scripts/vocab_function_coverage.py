import argparse
import csv
import json
import os
import re
import sys
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent

DEFAULT_VOCAB = str(SKILL_DIR / "assets" / "\u8bcd\u8868.txt")
DEFAULT_AUDIO_DIR = str(SKILL_DIR / "assets" / "MP3")
DEFAULT_OUT_DIR = str(SKILL_DIR / "result_vocab_function")
DEFAULT_WAKE_INDEXES = {1, 2}
DEFAULT_ANTI_START_INDEX = 295


def read_vocab(path):
    words = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        word = line.strip()
        if word:
            words.append(word)
    return words


class SerialCollector:
    def __init__(self, port, baud, log_path):
        import serial

        self.port = port
        self.baud = baud
        self.log_path = log_path
        self.serial = serial.Serial(port=port, baudrate=baud, timeout=0.2)
        self.lock = threading.Lock()
        self.lines = []
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.log_file = log_path.open("w", encoding="utf-8", newline="")

    def start(self):
        self.thread.start()

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=2)
        try:
            self.serial.close()
        finally:
            self.log_file.close()

    def _run(self):
        while not self.stop_event.is_set():
            try:
                raw = self.serial.readline()
            except Exception as exc:
                self._append_line(f"[serial-read-error] {exc}")
                time.sleep(0.2)
                continue
            if not raw:
                continue
            text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            self._append_line(text)

    def _append_line(self, text):
        item = {
            "mono": time.monotonic(),
            "host": datetime.now().strftime("%Y/%m/%d %H:%M:%S.%f")[:-3],
            "line": text,
        }
        with self.lock:
            self.lines.append(item)
        self.log_file.write(f"[{item['host']}] {text}\n")
        self.log_file.flush()

    def snapshot(self, start_mono, end_mono):
        with self.lock:
            return [x for x in self.lines if start_mono <= x["mono"] <= end_mono]


def setup_pygame():
    os.environ.setdefault("SDL_AUDIODRIVER", "directsound")
    import pygame

    pygame.mixer.init()
    return pygame


def play_audio(pygame, audio_path):
    start = time.monotonic()
    pygame.mixer.music.load(str(audio_path))
    pygame.mixer.music.play()
    while pygame.mixer.music.get_busy():
        time.sleep(0.05)
    end = time.monotonic()
    return start, end


def extract_matches(lines):
    recognized = []
    submitted = []
    wake_lines = []
    mode1_lines = []
    mode0_lines = []
    for item in lines:
        line = item["line"]
        if "WAKE(" in line:
            wake_lines.append(line)
        if "wakeup switch mode to: 1" in line or "MODE=1" in line:
            mode1_lines.append(line)
        if "wakeup switch mode to: 0" in line or "MODE=0" in line:
            mode0_lines.append(line)
        m = re.search(r"Recognized offline command:\s*(.+)$", line)
        if m:
            recognized.append(m.group(1).strip())
        m = re.search(r"Offline command submitted:\s*(.+)$", line)
        if m:
            submitted.append(m.group(1).strip())
    return {
        "wake": wake_lines,
        "mode1": mode1_lines,
        "mode0": mode0_lines,
        "recognized": recognized,
        "submitted": submitted,
    }


def summarize_result(case_type, matches):
    if case_type == "wake":
        return "PASS" if matches["wake"] else "FAIL_NO_WAKE"
    if case_type == "anti":
        if matches["wake"]:
            return "FAIL_FALSE_WAKE"
        if matches["recognized"] or matches["submitted"]:
            return "FAIL_FALSE_COMMAND"
        return "PASS"
    if matches["recognized"]:
        return "PASS"
    if matches["submitted"]:
        return "WARN_SUBMITTED_ONLY"
    return "FAIL_NO_COMMAND"


def classify_case(index, anti_start_index):
    if index in DEFAULT_WAKE_INDEXES:
        return "wake"
    if index >= anti_start_index:
        return "anti"
    return "command"


def write_csv(path, rows):
    fields = [
        "index",
        "word",
        "case_type",
        "status",
        "audio_file",
        "wake_played",
        "serial_lines",
        "wake_hits",
        "mode1_hits",
        "recognized_hits",
        "submitted_hits",
        "recognized_text",
        "submitted_text",
        "start_host",
        "end_host",
        "note",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def write_summary(path, summary):
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vocab", default=DEFAULT_VOCAB)
    parser.add_argument("--audio-dir", default=DEFAULT_AUDIO_DIR)
    parser.add_argument("--port", default="COM26")
    parser.add_argument("--baud", type=int, default=921600)
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--anti-start-index", type=int, default=DEFAULT_ANTI_START_INDEX)
    parser.add_argument("--wake-gap", type=float, default=1.2)
    parser.add_argument("--post-gap", type=float, default=1.8)
    parser.add_argument("--anti-cooldown", type=float, default=15.5)
    parser.add_argument("--max-cases", type=int, default=0)
    parser.add_argument("--skip-wake-before-command", action="store_true")
    args = parser.parse_args()

    root = Path.cwd()
    vocab_path = Path(args.vocab)
    if not vocab_path.is_absolute():
        vocab_path = root / vocab_path
    audio_dir = Path(args.audio_dir)
    if not audio_dir.is_absolute():
        audio_dir = root / audio_dir
    out_root = Path(args.out_dir)
    if not out_root.is_absolute():
        out_root = root / out_root

    words = read_vocab(vocab_path)
    if args.max_cases:
        words = words[: args.max_cases]

    audio_by_word = {p.stem: p for p in audio_dir.glob("*.mp3")}
    wake_word = words[1] if len(words) >= 2 else words[0]
    wake_audio = audio_by_word.get(wake_word)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = out_root / timestamp
    out_dir.mkdir(parents=True, exist_ok=True)
    serial_log = out_dir / f"{args.port.replace('COM', 'COM').zfill(5)}_{args.baud}_{datetime.now().strftime('%Y-%m-%d_%H_%M_%S')}.log"
    csv_path = out_dir / "vocab_function_coverage.csv"
    json_path = out_dir / "vocab_function_coverage.summary.json"

    rows = []
    missing_audio = []
    pygame = None
    collector = None
    started_at = datetime.now()

    try:
        pygame = setup_pygame()
        collector = SerialCollector(args.port, args.baud, serial_log)
        collector.start()
        time.sleep(1.5)

        for index, word in enumerate(words, 1):
            case_type = classify_case(index, args.anti_start_index)
            audio_path = audio_by_word.get(word)
            if audio_path is None:
                missing_audio.append({"index": index, "word": word})
                rows.append(
                    {
                        "index": index,
                        "word": word,
                        "case_type": case_type,
                        "status": "FAIL_MISSING_AUDIO",
                        "note": "mp3 missing",
                    }
                )
                continue

            wake_played = False
            if case_type == "anti":
                time.sleep(args.anti_cooldown)

            case_start = time.monotonic()
            start_host = datetime.now().strftime("%Y/%m/%d %H:%M:%S")

            if (
                case_type == "command"
                and not args.skip_wake_before_command
                and wake_audio is not None
            ):
                wake_played = True
                play_audio(pygame, wake_audio)
                time.sleep(args.wake_gap)

            target_start = time.monotonic()
            play_audio(pygame, audio_path)
            time.sleep(args.post_gap)
            case_end = time.monotonic()
            end_host = datetime.now().strftime("%Y/%m/%d %H:%M:%S")

            all_lines = collector.snapshot(case_start, case_end + 0.2)
            target_lines = collector.snapshot(target_start, case_end + 0.2)
            match_window = target_lines if case_type == "command" else all_lines
            matches = extract_matches(match_window)
            status = summarize_result(case_type, matches)

            rows.append(
                {
                    "index": index,
                    "word": word,
                    "case_type": case_type,
                    "status": status,
                    "audio_file": str(audio_path),
                    "wake_played": wake_played,
                    "serial_lines": len(all_lines),
                    "wake_hits": len(matches["wake"]),
                    "mode1_hits": len(matches["mode1"]),
                    "recognized_hits": len(matches["recognized"]),
                    "submitted_hits": len(matches["submitted"]),
                    "recognized_text": " | ".join(matches["recognized"]),
                    "submitted_text": " | ".join(matches["submitted"]),
                    "start_host": start_host,
                    "end_host": end_host,
                    "note": "",
                }
            )
            print(
                f"{index:03d}/{len(words):03d} {case_type} {status} "
                f"rec={len(matches['recognized'])} wake={len(matches['wake'])} {word}",
                flush=True,
            )
    finally:
        if collector is not None:
            collector.close()
        if pygame is not None:
            pygame.mixer.quit()

    finished_at = datetime.now()
    counts = Counter(row["status"] for row in rows)
    by_type = {}
    for row in rows:
        by_type.setdefault(row["case_type"], Counter())
        by_type[row["case_type"]][row["status"]] += 1

    summary = {
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": finished_at.isoformat(timespec="seconds"),
        "elapsed_s": round((finished_at - started_at).total_seconds(), 3),
        "vocab_path": str(vocab_path),
        "audio_dir": str(audio_dir),
        "out_dir": str(out_dir),
        "serial_log": str(serial_log),
        "csv": str(csv_path),
        "port": args.port,
        "baud": args.baud,
        "total_cases": len(rows),
        "status_counts": dict(counts),
        "type_status_counts": {k: dict(v) for k, v in by_type.items()},
        "missing_audio": missing_audio,
        "failures": [
            {
                "index": row["index"],
                "word": row["word"],
                "case_type": row["case_type"],
                "status": row["status"],
                "recognized_text": row.get("recognized_text", ""),
                "submitted_text": row.get("submitted_text", ""),
            }
            for row in rows
            if not str(row["status"]).startswith("PASS")
        ],
    }
    write_csv(csv_path, rows)
    write_summary(json_path, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.exit(main())
