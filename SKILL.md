---
name: arcs-firstwakeasr
description: 离线语音首唤醒窗口压测技能。Use when Codex or a tester needs to configure one local serial port, read device logs, play 本机默认声卡 wav 音频（你好小奥、打开浴霸、关闭浴霸）, 验证 reboot 后首次唤醒进入 mode=1 后的首次识别窗口内再次唤醒、命令识别、wakeup timeout、超时后恢复，并分离实时保存 Xshell 风格串口日志和工具执行日志。
---

# ARCS 离线语音首唤醒窗口压测

## 启动规则

1. 每次开始任务先读取当前目录 `plan.md`；不存在则创建。
2. 先把本轮要做什么、已做什么、未做什么写入 `plan.md`。
3. 执行过程中出现关键结果、阻塞、失败、未复现结论，都要及时追加到 `plan.md`。
4. 不要删除已有有效信息；只追加或整理明确过时/乱码内容。
5. 本技能目录就是当前目录，不要把技能复制到 Codex 技能目录。

## 下载后运行准备

1. 安装 Python 3.10+，并确认 `python` 可在 PowerShell 中执行。
2. 安装 Python 依赖：`python -m pip install -r requirements.txt`。
3. 安装 `ffmpeg` 并加入 `PATH`，脚本会用它把 16k/mono wav 预转换为 44.1k/stereo 播放文件。
4. 只修改 `config/arcs_config.json` 中的 `serial.port` 和 `serial.baud` 即可切换串口；默认示例为 `COM28@921600`。
5. 保持 `playback.use_default_soundcard_only=true`，脚本只用本机默认声卡，不指定其他声卡。
6. 执行前确认默认声卡音量可外放；脚本默认把系统音量设置为配置里的 `playback.volume=60`。

快速验证命令：

```powershell
python .\scripts\debug_serial_realtime_log.py --lines 10
python .\scripts\noise_aware_reboot_pressure.py --cycles 5
```

## 固定环境

- 配置文件：`config\arcs_config.json`。
- 串口：默认从配置文件读取 `serial.port` 与 `serial.baud`，当前为 `COM28@921600`。
- 后续修改串口号或波特率时，只改 `config\arcs_config.json`，不要直接改脚本。
- 播放：只使用本机 Windows 默认声卡，不指定其他声卡、不使用 device key。
- 音量：默认从配置文件读取 `playback.volume`，当前为 60，确认未静音。
- 唤醒词：默认从配置文件读取 `audio.wake_file`，当前为 `wav\你好小奥.wav`。
- 命令词：默认从配置文件读取 `audio.open_file`、`audio.close_file`，当前为 `wav\打开浴霸.wav`、`wav\关闭浴霸.wav`。
- 结果目录：默认从配置文件读取 `logging.out_dir`，每次任务创建 `result\YYYYMMDD_HHMMSS`。
- 压测重试：默认从配置文件读取 `pressure.retry_until_timeout=true`；命令词 miss 或首次窗口再次唤醒 miss 后，持续播报唤醒词直到看到 `mode=0`/`wakeup timeout`，`pressure.timeout_wait_seconds` 只作为防死锁保护。

## 日志硬约束

- 一次任务必须至少输出两个实时日志文件：Xshell 风格串口日志 `COM028_921600_yyyy-MM-dd_HH_mm_ss.log` 和工具执行日志 `*.tool.log`。
- 串口日志必须按用户提供的 Xshell 样例保存：UTF-8 BOM、CRLF 换行、`[BEGIN] yyyy/M/d HH:mm:ss`、`[yyyy/M/d HH:mm:ss.mmm] Connecting to COM28...`、`Connected.`、每条串口输出前缀 `[yyyy/M/d HH:mm:ss.mmm] `、结束 `Connection closed.` 和 `[END] yyyy/M/d HH:mm:ss`。
- 串口日志保存终端处理后的可读文本：去掉 ANSI ESC 颜色/清屏控制符，按串口原始换行拆成一条串口输出一行，便于人工阅读；不要混入工具事件、播放事件、统计事件。
- 串口日志必须覆盖任务开始到结束期间的串口输出；不要只保存关键行或匹配行。
- 工具执行日志只保存工具动作、播放、等待匹配、统计、异常、分析结论等执行信息；不要混入串口原始数据。
- 工具执行日志必须足够复盘：记录配置读取、音量设置、音频归一化、串口打开、reboot、播放开始/结束、等待匹配、计数统计、噪音探测、每轮结论和最终汇总。
- 两类日志每一行都必须以毫秒级时间戳开头，格式如 `[2026-06-08T19:17:18.064]`。
- 两类日志都必须实时追加保存：每条记录写入后立即 `flush`，默认还要 `fsync`。
- 串口记录内部的 `\r` 要转义成可见文本 `\\r`，保证一条串口记录对应一个物理日志行。
- 如果任务被中断，已经采集到的串口日志和工具日志都必须能恢复证据。
- 需要调试日志保存时运行：

```powershell
python .\scripts\debug_serial_realtime_log.py --lines 10
```

验收口径：任务目录有两个文件，`COM028_921600_yyyy-MM-dd_HH_mm_ss.log` 为 Xshell 风格串口日志，`serial_realtime_save_10lines.tool.log` 记录工具开始、串口读取统计、结束汇总；串口日志格式必须与 `D:\Xshell_Log\COM028_921600_2026-06-08_18_20_17.log` 对齐。

## 核心日志标记

- 空闲：`I/wakeup_algo counter_avg=...`、`ListenAI:/$`。
- 进入识别模式：`wakeup switch mode to: 1`、`MODE=1`、`wakeup_algo_switch_mode_cb: mode=1`。
- 唤醒词成功：`WAKE(1): ... KEY=2(ni3 hao3 xiao3 ao4)` 或 JSON 中 `keyword":"ni3 hao3 xiao3 ao4`。
- 打开浴霸成功：`Recognized offline command: kai yu ba` 或 `Offline command submitted: kai yu ba`。
- 关闭浴霸成功：`Recognized offline command: guan yu ba` 或 `Offline command submitted: guan yu ba`。
- 超时退出：`wakeup switch mode to: 0`、`wakeup timeout`、`wakeup_algo_timeout_cb`、`MODE=0`。

## 首次窗口目标异常判定逻辑

1. 单次命令词不识别不能直接判定设备异常，因为当前环境可能有噪音干扰。
2. 每轮必须从串口发送 `reboot` 开始，等待设备恢复 `ListenAI:/$` 或 `wakeup_algo` 日志。
3. 播放 `你好小奥`，必须看到进入识别模式标记 `mode=1`；否则判定 `fail_initial_wake_no_mode1`。
4. 进入首次识别窗口后，使用统一探针循环播放 `你好小奥`、`打开浴霸`、`关闭浴霸`，同时验证“再次唤醒”和“两条命令识别”。
5. 首轮探针中再次唤醒和两条命令都成功时，记为 `pass_first_window_wake_again_and_commands_recognized`。
6. 如果首轮未完整通过，但首次窗口内任一唤醒或命令有识别，说明不是目标异常；按噪音/播放疑似记为 `SUSPECT_NOISE` 类结果。
7. 只有首次窗口内持续到 `mode=0`/`wakeup timeout` 前 `wake_detected_count=0` 且 `command_recognized_count=0`，才是目标失败候选。
8. 目标失败候选在超时后重新唤醒并且 `打开浴霸`、`关闭浴霸` 均恢复识别时，标记为 `fail_first_window_no_wake_no_command_until_timeout_recovered`。
9. 目标失败候选在超时后仍无法重新唤醒或命令仍不识别，标记为恢复失败类问题。
10. 如果未看到超时且达到 `pressure.timeout_wait_seconds` 防死锁上限，标记为 `BLOCKED` 或 `SUSPECT`，不要伪造 FAIL。
11. 报告时必须区分 `PASS`、`SUSPECT_NOISE`、`FAIL`、`BLOCKED`，并列出探针轮数、唤醒命中数、命令命中数、是否超时和恢复结果。

## 推荐命令

执行 20 轮噪音感知压测：

```powershell
python .\scripts\noise_aware_reboot_pressure.py --cycles 20
```

执行到指定本地时间的长压测：

```powershell
python .\scripts\noise_aware_reboot_pressure.py --cycles 999999 --end-at 2026-06-09T08:30:00 --no-final-timeout
```

执行 10 条日志保存调试：

```powershell
python .\scripts\debug_serial_realtime_log.py --lines 10
```

## 交付要求

- 汇总本次结果目录、串口日志、工具执行日志、摘要 JSON、clean log。
- 必须分别列出 Xshell 风格串口日志和工具执行日志。
- 明确统计总轮数、进入识别模式成功数、打开识别成功数、关闭识别成功数、噪音探测触发次数、失败次数。
- 如未复现异常，写明“本轮未复现”，并说明仍可通过更多轮数或随机间隔提高置信度。
- 如出现命令 miss，必须展示噪音探测过程：miss 的命令、唤醒词重试次数、是否看到唤醒标记、是否超时退出、超时后恢复是否正常。
- 所有结论同步写入 `plan.md`。
