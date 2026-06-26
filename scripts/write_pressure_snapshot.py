import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path


def read_json(path):
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    csv_path = run_dir / "overnight_pressure_results.csv"
    summary_path = run_dir / "overnight_pressure.summary.json"
    snapshot_path = run_dir / "current_status_snapshot.md"

    rows = []
    if csv_path.exists():
        with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
    summary = read_json(summary_path)

    status_counts = Counter(row.get("status", "") for row in rows)
    failures = [row for row in rows if not row.get("status", "").startswith("PASS")]
    failure_words = Counter((row.get("word", ""), row.get("status", "")) for row in failures)
    last = rows[-1] if rows else {}

    lines = [
        "# 当前夜间压测状态快照",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 压测状态：`{summary.get('status', 'unknown')}`",
        f"- 目标结束时间：`{summary.get('target_end_at', '')}`",
        f"- 音量：`{summary.get('volume', '')}`",
        f"- 当前轮次：`{summary.get('cycle', last.get('cycle', ''))}`",
        f"- 当前全局序号：`{summary.get('global_index', last.get('global_index', ''))}`",
        f"- CSV 已写入行数：`{len(rows)}`",
        f"- 最后一条：`{last.get('global_index', '')}` / `cycle={last.get('cycle', '')}` / `case={last.get('case_index', '')}` / `{last.get('word', '')}` / `{last.get('status', '')}`",
        "",
        "## 状态统计",
        "",
        "| 状态 | 数量 |",
        "| --- | ---: |",
    ]
    for status, count in sorted(status_counts.items()):
        lines.append(f"| {status} | {count} |")
    lines.extend(["", "## 风险词聚合", "", "| 词条 | 状态 | 次数 |", "| --- | --- | ---: |"])
    if failure_words:
        for (word, status), count in sorted(failure_words.items(), key=lambda x: (-x[1], x[0][0])):
            lines.append(f"| {word} | {status} | {count} |")
    else:
        lines.append("| 无 | 无 | 0 |")
    lines.extend(["", "## 最近 10 条", "", "| 全局序号 | 轮次 | 用例 | 模块 | 词条 | 状态 |", "| ---: | ---: | ---: | --- | --- | --- |"])
    for row in rows[-10:]:
        lines.append(
            f"| {row.get('global_index', '')} | {row.get('cycle', '')} | {row.get('case_index', '')} | "
            f"{row.get('module', '')} | {row.get('word', '')} | {row.get('status', '')} |"
        )
    snapshot_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(snapshot_path)


if __name__ == "__main__":
    main()
