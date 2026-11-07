---
name: arcs-aopu-test
description: "奥普/ARCS 离线语音验证与压测技能。Use when Codex or a tester needs to run local serial-log based wakeup/ASR validation, reboot first-wakeup stress tests, full-vocabulary MP3 coverage, overnight pressure tests, threshold protocol switching, threshold-timeout algorithm checks, or mixed voice/reboot/threshold pressure tests with bundled test audio resources."
---

# ARCS 奥普离线语音验证与压测

## 启动规则

1. 每次开始任务先读取当前目录 `plan.md`；不存在则创建。
2. 先把本轮要做什么、已做什么、未做什么写入 `plan.md`。
3. 执行过程中出现关键结果、阻塞、失败、未复现结论，都要及时追加到 `plan.md`。
4. 不要删除已有有效信息；只追加或整理明确过时/乱码内容。
5. 本技能目录就是当前目录，优先在本目录内运行脚本，不要把技能复制到其他目录后再改路径。

## 适用设备

| 项 | 本 skill 的适用值 | 说明 |
|---|---|---|
| 项目 | 奥普/ARCS 离线语音（`ARCS-aopu`） | 只服务奥普 ARCS 项目 |
| 日志串口 | `COM26@921600` | 唤醒/识别判定与重启窗口都依赖这一路日志 |
| 协议/门限串口 | `COM28@9600` | 只用于 1/2/3 档门限协议下发与超时算法验证 |
| 语料 | `assets/MP3` 308 个 MP3 + `assets/词表.txt` 308 条 | 含唤醒词、命令词与反集词；只对奥普项目有效 |
| 不适用 | 非奥普 ARCS 设备、没有独立协议串口的设备 | TCL 空调用 `tcl-test-sub`，通用真机 BDD 用 `Polaris`，烧录用 `cskflashtest` |

## 能力范围

本 skill 已集成当前奥普项目可用的验证和压测逻辑：

- 首唤醒窗口重启压测：重启设备、等待 `Application Ready!!!`、使用 `assets\MP3` 音频验证首次唤醒进入 `mode=1`、首次识别窗口内再次唤醒和命令识别、超时后恢复。
- 全词表功能覆盖：按 `assets\词表.txt` 播放 `assets\MP3` 中的全量音频，覆盖唤醒词、命令词和反集词，并输出 CSV/JSON。
- 夜间语音压测：按模块化用例长时间循环播放，覆盖核心功能、失败项复测、反集误唤醒和安全收尾。
- 协议门限切换验证：通过 `COM28@9600` 发送 1/2/3 档门限协议，并用 `COM26@921600` 复核唤醒和命令识别。
- 三档门限超时重唤醒算法验证：每档设置后等待超时，再重新唤醒、命令识别，并提取算法门限字段。
- 组合压测：支持语音交互、重启唤醒、三档门限的单项、两两组合、三项组合和 mixed 随机混合长稳压测。
- 结果汇总：支持夜间压测报告、运行快照、串口日志和 CSV/JSON 证据输出。

## 资源结构

- `assets\MP3`：奥普全词表 MP3 测试资源，当前 308 个 `.mp3`，用于全词表、夜间压测、门限验证和组合压测。
- `assets\词表.txt`：与 MP3 对应的去重词表，当前 308 条。
- 音频为静态语料：不重生成、不重编码；需要补录时用 `scripts/regen_audio.py` 输出到 `result/audio_regen/<日期>/`，说明见 `README.md` 的“音频来源与重生成”。
- `config\arcs_config.json`：首唤醒窗口重启压测和串口日志调试的默认配置。
- `scripts`：所有可执行脚本。脚本默认优先使用本 skill 内置资源路径。
- `run_aopu_combo_pressure.bat`：组合压测批处理入口，只需改顶部参数即可运行。

## 音频来源与重生成

- `assets/MP3`：308 个奥普全词表 MP3，与 `assets/词表.txt`（308 行）逐条对应；文件名即播放文本。
- 原工程未保留这批音频的 TTS 供应商、模型、音色和合成参数，本仓库也没有音频 manifest；不补造不可验证的信息，需要哈希与格式时用重生成脚本产出的 manifest 对照（补静态 manifest 是后续任务）。
- 这批音频是**静态语料**：不重生成、不重编码、不改名；新增、替换或删除音频后必须同步 `assets/词表.txt` 与全词表覆盖统计，并重跑全词表验证。

需要补录或用统一音色重做时，用 `scripts/regen_audio.py` 输出到 `result/audio_regen/<日期>/` 再人工比对，不覆盖 `assets/MP3`。

安装 / 更新 lstts：

```bash
pip install "lstts-core @ git+https://github.com/zjashanda/lstts.git@main"
python -c "import lstts_core; print(lstts_core.__version__)"
```

```bash
# 只看计划（不装 lstts、不访问网络）
python scripts/regen_audio.py plan

# 合成前 5 条到 result/audio_regen/<时间戳>/
python scripts/regen_audio.py build --limit 5
```

- 输出目录默认 `result/audio_regen/<时间戳>/`；`--corpus-dir assets/MP3` 命中即拒绝。
- 每条产物写 `regen_manifest.json` / `.csv`（文本、音色、采样率、时长、响度、体积、SHA-256、状态）。
- 凭据只用 `LSTTS_TTS_TOKEN`（兼容旧 `LSTEST_TTS_TOKEN`），不写进仓库和日志。
- 离线自测：`python -m unittest discover -s tests`。

## 环境准备

1. 安装 Python 3.10+，确认 `python` 可在 PowerShell/cmd 中执行。
2. 安装依赖：`python -m pip install -r requirements.txt`。
3. 首唤醒窗口重启压测默认使用 `assets\MP3` 中的 MP3 资源；脚本会通过 `ffmpeg` 归一化为临时 WAV 播放文件，因此需要安装 `ffmpeg` 并加入 `PATH`。
4. Windows 默认声卡需要可外放；奥普当前压测默认音量为 20，可通过参数修改。
5. 默认日志串口为 `COM26@921600`；默认协议串口为 `COM28@9600`。如设备枚举变化，优先通过参数或批处理顶部变量修改。

## 固定日志和识别标记

- 空闲：`I/wakeup_algo counter_avg=...`、`ListenAI:/$`。
- 应用就绪：`Application Ready!!!`。
- 进入识别模式：`wakeup switch mode to: 1`、`MODE=1`、`wakeup_algo_switch_mode_cb: mode=1`。
- 唤醒成功：`WAKE(1): ... KEY=2(ni3 hao3 xiao3 ao4)` 或 JSON 中 `keyword":"ni3 hao3 xiao3 ao4`。
- 打开浴霸成功：`Recognized offline command: kai yu ba` 或 `Offline command submitted: kai yu ba`。
- 关闭浴霸成功：`Recognized offline command: guan yu ba` 或 `Offline command submitted: guan yu ba`。
- 超时退出：`wakeup switch mode to: 0`、`wakeup timeout`、`wakeup_algo_timeout_cb`、`MODE=0`。

日志要求：

- 串口日志必须实时保存，不能只保存匹配到的关键行。
- 工具执行日志、CSV、JSON、报告都要保留在结果目录，便于复盘。
- 如果任务被中断，已经采集到的串口日志和工具日志必须仍可作为证据。
- 结论必须区分 `PASS`、`WARN`、`SUSPECT`、`FAIL`、`BLOCKED`，不要把脚本未进入有效流程直接当作产品失败。

## 快速检查

在 skill 根目录执行：

```powershell
python .\scripts\debug_serial_realtime_log.py --lines 10
python .\scripts\vocab_function_coverage.py --max-cases 3 --port COM26 --baud 921600
cmd /c "set AOPU_DRY_RUN=1&& call run_aopu_combo_pressure.bat <nul"
```

## 首唤醒窗口重启压测

用途：验证 reboot 后首次唤醒窗口内是否能再次唤醒和识别 `打开浴霸`、`关闭浴霸`，并复核超时后恢复。

推荐命令：

```powershell
python .\scripts\noise_aware_reboot_pressure.py --cycles 20 --port COM26 --baud 921600 --volume 20
python .\scripts\noise_aware_reboot_pressure.py --cycles 999999 --end-at 2026-06-27T08:00:00 --no-final-timeout --port COM26 --baud 921600 --volume 20
```

判定要点：

1. 每轮从串口发送 `reboot` 开始，等待 `Application Ready!!!` 或后续 wakeup 日志。
2. Initial wake 不做单次判死，按配置重试到 `mode=1` 或达到上限。
3. 首次窗口中只要任一唤醒或命令有识别，应优先归为非目标异常或疑似环境问题。
4. 只有窗口内持续到 `mode=0`/`wakeup timeout` 前唤醒和命令都未识别，且超时后恢复正常，才归入目标失败候选。

## 全词表功能覆盖

用途：按去重词表逐条播放 MP3，检查唤醒词、命令词和反集词是否符合预期。

```powershell
python .\scripts\vocab_function_coverage.py --port COM26 --baud 921600
python .\scripts\vocab_function_coverage.py --port COM26 --baud 921600 --max-cases 20
```

默认资源：

- 词表：`assets\词表.txt`。
- 音频：`assets\MP3`。
- 输出：`result_vocab_function\YYYYMMDD_HHMMSS`。

## 夜间语音压测

用途：长时间覆盖唤醒稳定性、基础功能、档位/温度/灯光/蓝牙/反集/失败项复测和安全收尾。

```powershell
python .\scripts\overnight_pressure.py --end-at 2026-06-27T08:00:00 --port COM26 --baud 921600 --volume 20
```

结束后生成报告：

```powershell
python .\scripts\summarize_pressure_results.py --run-dir .\result_overnight_pressure\[运行目录]
```

## 三档门限协议验证

当前协议帧：

- 1 档：`A5 08 02 00 EE 01 00 9E`，期望回包 `A5 08 01 00 EE 01 00 9E`。
- 2 档：`A5 08 02 00 EE 02 00 9F`，期望回包 `A5 08 01 00 EE 02 00 9F`。
- 3 档：`A5 08 02 00 EE 03 00 A0`，当前按推算协议长期发送验证；即使算法外显数值与 2 档接近，也不要跳过 3 档协议。

基础切换验证：

```powershell
python .\scripts\threshold_switch_probe.py --protocol-port COM28 --protocol-baud 9600 --log-port COM26 --log-baud 921600
```

超时后重唤醒和算法门限验证：

```powershell
python .\scripts\threshold_timeout_algorithm_probe.py --cycles 3 --protocol-port COM28 --protocol-baud 9600 --log-port COM26 --log-baud 921600
```

判定要点：

- 协议层必须记录发送帧、回包、回包长度和每档统计。
- 功能层必须验证每档设置后唤醒和 `打开浴霸`/`关闭浴霸` 识别。
- 算法层重点观察 `1414`、`1410`、`2121/2122`、`ncmThreshold`、`wakeup_algo_set_threshold` 等字段。
- 当前经验：1 档可见门限与 2/3 档有差异，2/3 档外显数值可能接近，但 3 档协议仍需发送以验证长稳协议稳定性。

## 组合压测

优先使用批处理入口：

```powershell
cmd /c run_aopu_combo_pressure.bat
```

批处理顶部 `MODE_ID` 含义：

- `1`：`voice-only`，纯语音唤醒 + 命令识别。
- `2`：`reboot-only`，单独重启 + App Ready + 首次唤醒。
- `3`：`threshold-only`，只发送 1/2/3 档协议和检查回包。
- `4`：`threshold-voice`，每档门限 + 超时 + 重唤醒 + 命令识别。
- `5`：`reboot-voice`，重启后语音交互。
- `6`：`reboot-threshold`，重启后发送三档门限协议。
- `7`：`reboot-threshold-voice`，重启 + 三档门限 + 每档语音交互。
- `8`：`mixed`，随机混合执行配置的模式，适合夜间综合稳定性压测。

也可直接执行 Python：

```powershell
python .\scripts\aopu_combo_pressure.py --mode mixed --cycles 9999 --end-at 2026-06-27T08:00:00 --log-port COM26 --log-baud 921600 --protocol-port COM28 --protocol-baud 9600 --volume 20
python .\scripts\aopu_combo_pressure.py --mode threshold-voice --cycles 3 --threshold-levels 1,2,3 --volume 20
```

## 结果交付要求

每次执行后至少汇总：

- 结果目录、运行模式、开始/结束时间、串口、音量、资源路径。
- 串口原始日志、CSV 明细、summary JSON、plan JSON 或工具日志路径。
- 总步骤、PASS/FAIL/WARN/BLOCKED 数量和通过率。
- 唤醒总次数、命令识别总次数、三档协议发送/回包总次数。
- 是否存在连续不能唤醒、连续不能识别、连续协议无回包、重启未触发、App Ready 超时。
- 对唯一/少量失败必须复核原始日志，区分设备异常、脚本触发失败、环境/外部依赖问题。
- 所有结论同步写入 `plan.md`。

## Resources

### scripts/

- `debug_serial_realtime_log.py`：串口日志实时保存格式检查。
- `noise_aware_reboot_pressure.py`：首唤醒窗口重启压测。
- `vocab_function_coverage.py`：全词表功能覆盖。
- `overnight_pressure.py`：夜间语音压测。
- `summarize_pressure_results.py`：夜间压测 Markdown 汇总。
- `write_pressure_snapshot.py`：运行中状态快照。
- `wait_and_summarize_overnight.ps1`：定时等待并自动生成夜间汇总。
- `threshold_switch_probe.py`：三档门限切换基础验证。
- `threshold_timeout_algorithm_probe.py`：三档门限超时重唤醒算法验证。
- `aopu_combo_pressure.py`：语音、重启、门限组合压测主入口。
- `set_default_render_volume.ps1`：Windows 默认播放音量设置。

### assets/

- `MP3/`：全词表 MP3 测试音频。
- `词表.txt`：全词表文本。

### config/

- `arcs_config.json`：首唤醒窗口重启压测默认串口、音量、音频和日志配置。
