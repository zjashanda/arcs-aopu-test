import argparse
import csv
import json
import signal
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from vocab_function_coverage import (  # noqa: E402
    SerialCollector,
    extract_matches,
    play_audio,
    setup_pygame,
    summarize_result,
)


def parse_end_at(value):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    raise argparse.ArgumentTypeError("end time must be 'YYYY-MM-DD HH:MM:SS'")


def case_plan():
    cases = []

    def add(module, word, case_type="command", note=""):
        cases.append({"module": module, "word": word, "case_type": case_type, "note": note})

    add("唤醒稳定性", "你好小奥", "wake", "主唤醒词")
    add("唤醒稳定性", "小奥同学", "wake", "备选唤醒词")

    for word in [
        "打开浴霸",
        "关闭浴霸",
        "打开取暖",
        "关闭取暖",
        "打开吹风",
        "关闭吹风",
        "打开换气",
        "关闭换气",
        "打开干燥",
        "关闭干燥",
        "打开照明",
        "关闭照明",
        "开灯",
        "关灯",
    ]:
        add("基础功能开关", word)

    for word in [
        "吹风调到一档",
        "吹风调到三档",
        "吹风调到五档",
        "换气调到一档",
        "换气调到三档",
        "换气调到五档",
        "调高风速",
        "调小风速",
    ]:
        add("档位/风速调节", word)

    for word in [
        "温度调到三十度",
        "温度调到三十六度",
        "温度调到四十二度",
        "调高温度",
        "调低温度",
    ]:
        add("温度槽位", word)

    for word in [
        "打开干区取暖",
        "关闭干区取暖",
        "打开单区AI暖",
        "关闭单区AI暖",
        "打开湿区AI暖",
        "关闭湿区AI暖",
        "打开双区AI暖",
        "关闭双区AI暖",
    ]:
        add("分区串扰风险", word)

    for word in [
        "打开除臭除菌",
        "关闭除臭除菌",
        "打开低噪换气",
        "关闭低噪换气",
        "打开快速换气",
        "关闭快速换气",
    ]:
        add("净化/换气模式", word)

    for word in [
        "打开氛围灯",
        "关闭氛围灯",
        "亮度调到百分之十",
        "亮度调到百分之一百",
        "打开红光",
        "关闭红光",
    ]:
        add("灯光/亮度/颜色", word)

    for word in [
        "暂停",
        "下一首",
        "上一首",
        "打开蓝牙",
        "关闭蓝牙",
    ]:
        add("音乐/蓝牙/快捷指令", word)

    for word in [
        "滴2声",
        "播放下一首",
        "关闭淋浴模式",
        "退出",
        "再见",
        "半小时以后关闭取暖",
        "今天有点冷，调下浴霸",
        "我要洗澡了，劲爆一点",
    ]:
        add("全词表失败项复测", word, "command", "下午全词表未识别项")

    for word in [
        "天猫精灵",
        "小爱同学",
        "小度小度",
        "小艺小艺",
        "小优小优",
        "小美小美",
        "小布小布",
        "小冰小冰",
        "小V小V",
        "小德小德",
        "小欧小欧",
        "小聚小聚",
        "小维小维",
        "小T小T",
    ]:
        add("反集误唤醒", word, "anti")

    for word in [
        "关闭浴霸",
        "关闭取暖",
        "关闭风暖",
        "关闭暖风",
        "关闭吹风",
        "关闭换气",
        "关闭干燥",
        "关闭照明",
    ]:
        add("安全收尾关闭", word, "command", "每轮收尾，降低夜间长时间开启风险")

    return cases


def read_vocab(path):
    return [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def ensure_cases_have_audio(cases, audio_by_word):
    missing = sorted({case["word"] for case in cases if case["word"] not in audio_by_word})
    if missing:
        raise FileNotFoundError("missing pressure audio: " + ", ".join(missing))


def write_csv_header(path):
    fields = [
        "global_index",
        "cycle",
        "case_index",
        "module",
        "word",
        "case_type",
        "status",
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
        csv.DictWriter(f, fieldnames=fields).writeheader()
    return fields


def append_csv(path, fields, row):
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writerow({field: row.get(field, "") for field in fields})


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vocab", default=str(SKILL_DIR / "assets" / "词表.txt"))
    parser.add_argument("--audio-dir", default=str(SKILL_DIR / "assets" / "MP3"))
    parser.add_argument("--port", default="COM26")
    parser.add_argument("--baud", type=int, default=921600)
    parser.add_argument("--out-dir", default=str(SKILL_DIR / "result_overnight_pressure"))
    parser.add_argument("--end-at", type=parse_end_at, required=True)
    parser.add_argument("--volume", type=int, default=20)
    parser.add_argument("--wake-word", default="你好小奥")
    parser.add_argument("--wake-gap", type=float, default=1.2)
    parser.add_argument("--post-gap", type=float, default=1.8)
    parser.add_argument("--anti-cooldown", type=float, default=15.5)
    args = parser.parse_args()

    root = Path.cwd()
    vocab_path = Path(args.vocab)
    if not vocab_path.is_absolute():
        vocab_path = root / vocab_path
    audio_dir = Path(args.audio_dir)
    if not audio_dir.is_absolute():
        audio_dir = root / audio_dir
    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = root / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    words = read_vocab(vocab_path)
    audio_by_word = {p.stem: p for p in audio_dir.glob("*.mp3")}
    cases = case_plan()
    ensure_cases_have_audio(cases, audio_by_word)
    wake_audio = audio_by_word[args.wake_word]

    csv_path = out_dir / "overnight_pressure_results.csv"
    summary_path = out_dir / "overnight_pressure.summary.json"
    cases_path = out_dir / "overnight_pressure_cases.json"
    heartbeat_path = out_dir / "heartbeat.txt"
    serial_log = out_dir / f"{args.port}_{args.baud}_{datetime.now().strftime('%Y-%m-%d_%H_%M_%S')}.log"

    fields = write_csv_header(csv_path)
    write_json(cases_path, {"cases": cases, "case_count": len(cases)})

    stop_requested = False

    def request_stop(signum, _frame):
        nonlocal stop_requested
        stop_requested = True
        print(f"stop requested by signal {signum}", flush=True)

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    started_at = datetime.now()
    rows = []
    counts = Counter()
    module_counts = {}
    cycle = 0
    global_index = 0
    pygame = None
    collector = None

    try:
        pygame = setup_pygame()
        collector = SerialCollector(args.port, args.baud, serial_log)
        collector.start()
        time.sleep(1.5)

        while datetime.now() < args.end_at and not stop_requested:
            cycle += 1
            for case_index, case in enumerate(cases, 1):
                if datetime.now() >= args.end_at or stop_requested:
                    break

                global_index += 1
                word = case["word"]
                case_type = case["case_type"]
                audio_path = audio_by_word[word]

                if case_type == "anti":
                    time.sleep(args.anti_cooldown)

                case_start = time.monotonic()
                start_host = datetime.now().strftime("%Y/%m/%d %H:%M:%S")

                if case_type == "command":
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

                row = {
                    "global_index": global_index,
                    "cycle": cycle,
                    "case_index": case_index,
                    "module": case["module"],
                    "word": word,
                    "case_type": case_type,
                    "status": status,
                    "serial_lines": len(all_lines),
                    "wake_hits": len(matches["wake"]),
                    "mode1_hits": len(matches["mode1"]),
                    "recognized_hits": len(matches["recognized"]),
                    "submitted_hits": len(matches["submitted"]),
                    "recognized_text": " | ".join(matches["recognized"]),
                    "submitted_text": " | ".join(matches["submitted"]),
                    "start_host": start_host,
                    "end_host": end_host,
                    "note": case.get("note", ""),
                }
                rows.append(row)
                counts[status] += 1
                module_counts.setdefault(case["module"], Counter())
                module_counts[case["module"]][status] += 1
                append_csv(csv_path, fields, row)

                if global_index % 10 == 0:
                    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    heartbeat_path.write_text(
                        f"{now} cycle={cycle} global_index={global_index} last={status} {word}\n",
                        encoding="utf-8",
                    )
                    write_json(
                        summary_path,
                        {
                            "status": "running",
                            "started_at": started_at.isoformat(timespec="seconds"),
                            "updated_at": now,
                            "target_end_at": args.end_at.isoformat(sep=" ", timespec="seconds"),
                            "volume": args.volume,
                            "cycle": cycle,
                            "global_index": global_index,
                            "status_counts": dict(counts),
                            "module_status_counts": {k: dict(v) for k, v in module_counts.items()},
                            "csv": str(csv_path),
                            "serial_log": str(serial_log),
                        },
                    )
                print(
                    f"{global_index} cycle={cycle} case={case_index}/{len(cases)} "
                    f"{case_type} {status} {word}",
                    flush=True,
                )
    finally:
        if collector is not None:
            collector.close()
        if pygame is not None:
            pygame.mixer.quit()

    finished_at = datetime.now()
    failures = [
        {
            "global_index": row["global_index"],
            "cycle": row["cycle"],
            "module": row["module"],
            "word": row["word"],
            "case_type": row["case_type"],
            "status": row["status"],
            "recognized_text": row["recognized_text"],
            "submitted_text": row["submitted_text"],
        }
        for row in rows
        if not str(row["status"]).startswith("PASS")
    ]
    write_json(
        summary_path,
        {
            "status": "finished" if not stop_requested else "stopped",
            "started_at": started_at.isoformat(timespec="seconds"),
            "finished_at": finished_at.isoformat(timespec="seconds"),
            "target_end_at": args.end_at.isoformat(sep=" ", timespec="seconds"),
            "elapsed_s": round((finished_at - started_at).total_seconds(), 3),
            "volume": args.volume,
            "port": args.port,
            "baud": args.baud,
            "vocab_path": str(vocab_path),
            "audio_dir": str(audio_dir),
            "out_dir": str(out_dir),
            "csv": str(csv_path),
            "serial_log": str(serial_log),
            "case_count_per_cycle": len(cases),
            "cycles_started": cycle,
            "total_cases": len(rows),
            "status_counts": dict(counts),
            "module_status_counts": {k: dict(v) for k, v in module_counts.items()},
            "failure_count": len(failures),
            "failures_head": failures[:100],
            "vocab_total": len(words),
        },
    )


if __name__ == "__main__":
    sys.exit(main())
