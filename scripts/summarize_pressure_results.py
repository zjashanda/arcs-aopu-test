import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


def read_json(path):
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_rows(path):
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def percent(part, total):
    if not total:
        return "0.00%"
    return f"{part / total * 100:.2f}%"


def status_ok(status):
    return str(status).startswith("PASS")


def write_report(run_dir, out_file):
    summary_path = run_dir / "overnight_pressure.summary.json"
    results_path = run_dir / "overnight_pressure_results.csv"
    cases_path = run_dir / "overnight_pressure_cases.json"
    heartbeat_path = run_dir / "heartbeat.txt"
    volume_path = run_dir / "volume_set.json"
    pid_path = run_dir / "pid.txt"
    console_err_path = run_dir / "console.err.log"

    summary = read_json(summary_path)
    cases = read_json(cases_path).get("cases", [])
    volume = read_json(volume_path)
    rows = read_rows(results_path)

    status_counts = Counter(row.get("status", "") for row in rows)
    module_counts = defaultdict(Counter)
    type_counts = defaultdict(Counter)
    for row in rows:
        module_counts[row.get("module", "")][row.get("status", "")] += 1
        type_counts[row.get("case_type", "")][row.get("status", "")] += 1

    total = len(rows)
    passed = sum(1 for row in rows if status_ok(row.get("status", "")))
    failures = [row for row in rows if not status_ok(row.get("status", ""))]
    false_wake = [row for row in failures if row.get("status") == "FAIL_FALSE_WAKE"]
    no_command = [row for row in failures if row.get("status") == "FAIL_NO_COMMAND"]

    heartbeat = heartbeat_path.read_text(encoding="utf-8", errors="replace").strip() if heartbeat_path.exists() else ""
    pid = pid_path.read_text(encoding="ascii", errors="ignore").strip() if pid_path.exists() else ""
    console_err_tail = ""
    if console_err_path.exists() and console_err_path.stat().st_size:
        console_err_tail = "\n".join(console_err_path.read_text(encoding="utf-8", errors="replace").splitlines()[-20:])

    serial_log = summary.get("serial_log")
    serial_size = ""
    if serial_log and Path(serial_log).exists():
        serial_size = str(Path(serial_log).stat().st_size)

    lines = []
    lines.append("# 奥普夜间压测结果报告")
    lines.append("")
    lines.append(f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- 结果目录：`{run_dir}`")
    lines.append(f"- 压测状态：`{summary.get('status', 'unknown')}`")
    lines.append(f"- 开始时间：`{summary.get('started_at', '')}`")
    lines.append(f"- 目标结束时间：`{summary.get('target_end_at', '')}`")
    lines.append(f"- 实际结束时间：`{summary.get('finished_at', '')}`")
    lines.append(f"- 播放音量：`{summary.get('volume', volume.get('requested_volume', ''))}`")
    lines.append(f"- COM 串口：`{summary.get('port', 'COM26')}`，波特率：`{summary.get('baud', 921600)}`")
    lines.append(f"- PID：`{pid}`")
    lines.append(f"- 最新心跳：`{heartbeat}`")
    lines.append("")
    lines.append("## 1. 总体统计")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("| --- | ---: |")
    lines.append(f"| 单轮用例数 | {len(cases) or summary.get('case_count_per_cycle', '')} |")
    lines.append(f"| 已执行用例数 | {total} |")
    lines.append(f"| 通过数 | {passed} |")
    lines.append(f"| 失败/风险数 | {len(failures)} |")
    lines.append(f"| 通过率 | {percent(passed, total)} |")
    lines.append(f"| 启动轮次 | {summary.get('cycles_started', summary.get('cycle', ''))} |")
    lines.append(f"| 串口日志字节数 | {serial_size} |")
    lines.append("")
    lines.append("## 2. 状态分布")
    lines.append("")
    lines.append("| 状态 | 数量 |")
    lines.append("| --- | ---: |")
    for status, count in sorted(status_counts.items()):
        lines.append(f"| {status} | {count} |")
    lines.append("")
    lines.append("## 3. 模块统计")
    lines.append("")
    lines.append("| 模块 | 总数 | 通过 | 失败/风险 | 通过率 |")
    lines.append("| --- | ---: | ---: | ---: | ---: |")
    for module, counts in module_counts.items():
        module_total = sum(counts.values())
        module_pass = sum(count for status, count in counts.items() if status_ok(status))
        lines.append(f"| {module} | {module_total} | {module_pass} | {module_total - module_pass} | {percent(module_pass, module_total)} |")
    lines.append("")
    lines.append("## 4. 类型统计")
    lines.append("")
    lines.append("| 类型 | 总数 | 通过 | 失败/风险 | 通过率 |")
    lines.append("| --- | ---: | ---: | ---: | ---: |")
    for case_type, counts in type_counts.items():
        type_total = sum(counts.values())
        type_pass = sum(count for status, count in counts.items() if status_ok(status))
        lines.append(f"| {case_type} | {type_total} | {type_pass} | {type_total - type_pass} | {percent(type_pass, type_total)} |")
    lines.append("")
    lines.append("## 5. 失败/风险明细")
    lines.append("")
    if failures:
        lines.append("| 全局序号 | 轮次 | 模块 | 词条 | 类型 | 状态 | 识别文本 | 提交文本 |")
        lines.append("| ---: | ---: | --- | --- | --- | --- | --- | --- |")
        for row in failures[:300]:
            lines.append(
                f"| {row.get('global_index', '')} | {row.get('cycle', '')} | "
                f"{row.get('module', '')} | {row.get('word', '')} | {row.get('case_type', '')} | "
                f"{row.get('status', '')} | {row.get('recognized_text', '')} | {row.get('submitted_text', '')} |"
            )
        if len(failures) > 300:
            lines.append(f"| ... | ... | ... | 仅展示前 300 条，完整见 CSV | ... | ... | ... | ... |")
    else:
        lines.append("- 暂未发现失败/风险项。")
    lines.append("")
    lines.append("## 6. 重点风险聚合")
    lines.append("")
    lines.append(f"- 反集误唤醒次数：{len(false_wake)}")
    lines.append(f"- 命令未识别次数：{len(no_command)}")
    if false_wake:
        words = Counter(row.get("word", "") for row in false_wake)
        lines.append("- 反集误唤醒词条：" + "，".join(f"{word}({count})" for word, count in words.items()))
    if no_command:
        words = Counter(row.get("word", "") for row in no_command)
        lines.append("- 命令未识别词条：" + "，".join(f"{word}({count})" for word, count in words.items()))
    lines.append("")
    lines.append("## 7. 文件索引")
    lines.append("")
    lines.append(f"- 汇总 JSON：`{summary_path}`")
    lines.append(f"- 明细 CSV：`{results_path}`")
    lines.append(f"- 用例清单：`{cases_path}`")
    lines.append(f"- 串口日志：`{summary.get('serial_log', '')}`")
    lines.append(f"- 控制台输出：`{run_dir / 'console.out.log'}`")
    lines.append(f"- 控制台错误：`{console_err_path}`")
    if console_err_tail:
        lines.append("")
        lines.append("## 8. 控制台错误尾部")
        lines.append("")
        lines.append("```text")
        lines.append(console_err_tail)
        lines.append("```")

    out_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    machine_summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "run_dir": str(run_dir),
        "status": summary.get("status", "unknown"),
        "total": total,
        "passed": passed,
        "failed_or_risk": len(failures),
        "pass_rate": percent(passed, total),
        "status_counts": dict(status_counts),
        "false_wake_count": len(false_wake),
        "no_command_count": len(no_command),
        "report": str(out_file),
    }
    (run_dir / "overnight_pressure.report_summary.json").write_text(
        json.dumps(machine_summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return machine_summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--out-file")
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    out_file = Path(args.out_file).resolve() if args.out_file else run_dir / "奥普夜间压测结果报告.md"
    result = write_report(run_dir, out_file)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
