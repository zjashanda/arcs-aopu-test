@echo off
setlocal EnableExtensions

REM ============================================================
REM Aopu combo pressure runner. Edit variables in this section.
REM Comments are ASCII-only so the batch can run under any Windows code page.
REM ============================================================

REM Pressure mode. Edit MODE_ID only.
REM   1 = voice-only             : wake + command recognition
REM   2 = reboot-only            : reboot + App Ready + first wake
REM   3 = threshold-only         : send level 1/2/3 threshold protocol only
REM   4 = threshold-voice        : threshold switch + timeout + wake + commands
REM   5 = reboot-voice           : reboot + voice interaction
REM   6 = reboot-threshold       : reboot + level 1/2/3 protocol send
REM   7 = reboot-threshold-voice : reboot + thresholds + voice per level
REM   8 = mixed                  : random modes from MIXED_MODES
set "MODE_ID=8"

REM Stop condition: stop when CYCLES finishes or END_AT is reached. END_AT can be empty.
REM Use T in END_AT to avoid quoting issues, for example 2026-06-26T08:00:00.
set "CYCLES=9999"
set "END_AT="

REM Serial ports. LOG_PORT reads algorithm logs and sends reboot; PROTOCOL_PORT sends threshold protocol.
set "LOG_PORT=COM26"
set "LOG_BAUD=921600"
set "PROTOCOL_PORT=COM28"
set "PROTOCOL_BAUD=9600"

REM Test resources and output directory. MP3 resources are bundled in assets\MP3.
set "AUDIO_DIR=assets\MP3"
set "OUT_DIR=result_combo_pressure"

REM PC playback volume. Set SKIP_VOLUME=1 to avoid changing system volume.
set "VOLUME=20"
set "SKIP_VOLUME=0"

REM Voice words. Leave empty to use Python defaults: nihao xiaoao / open yuba,close yuba.
set "WAKE_WORD="
set "COMMANDS="

REM Threshold protocol levels. Keep 1,2,3 for long-run protocol stability validation.
set "THRESHOLD_LEVELS=1,2,3"

REM For threshold-voice and reboot-threshold-voice: wait timeout before waking again.
set "THRESHOLD_TIMEOUT=1"
set "TIMEOUT_WAIT=22"

REM Timing parameters.
set "POST_GAP=2.0"
set "WAKE_GAP=1.2"
set "GAP=1.0"
set "BOOT_TIMEOUT=45"
set "POST_REBOOT_GRACE=3"
set "PROTOCOL_READ_SECONDS=1.5"

REM Mixed mode pool. Repeat one mode to increase its random weight.
set "MIXED_MODES=voice-only,threshold-voice,reboot-voice,reboot-threshold-voice"
set "RANDOM_SEED=20260625"

REM DRY_RUN=1 prints the command only. You can also set AOPU_DRY_RUN=1 outside this file.
set "DRY_RUN=0"
if not "%AOPU_DRY_RUN%"=="" set "DRY_RUN=%AOPU_DRY_RUN%"

REM ============================================================
REM Usually do not edit below this line.
REM ============================================================

pushd "%~dp0"

set "PYTHON_EXE=python"
set "SCRIPT_PATH=scripts\aopu_combo_pressure.py"

set "MODE="
if "%MODE_ID%"=="1" set "MODE=voice-only"
if "%MODE_ID%"=="2" set "MODE=reboot-only"
if "%MODE_ID%"=="3" set "MODE=threshold-only"
if "%MODE_ID%"=="4" set "MODE=threshold-voice"
if "%MODE_ID%"=="5" set "MODE=reboot-voice"
if "%MODE_ID%"=="6" set "MODE=reboot-threshold"
if "%MODE_ID%"=="7" set "MODE=reboot-threshold-voice"
if "%MODE_ID%"=="8" set "MODE=mixed"

if "%MODE%"=="" (
  echo ERROR: unsupported MODE_ID=%MODE_ID%
  echo Please set MODE_ID to 1,2,3,4,5,6,7 or 8.
  popd
  pause
  exit /b 1
)

set "END_AT_ARG="
if not "%END_AT%"=="" set "END_AT_ARG=--end-at %END_AT%"

set "VOLUME_ARG=--volume %VOLUME%"
if "%SKIP_VOLUME%"=="1" set "VOLUME_ARG=--skip-volume"

set "THRESHOLD_TIMEOUT_ARG=--threshold-timeout"
if "%THRESHOLD_TIMEOUT%"=="0" set "THRESHOLD_TIMEOUT_ARG=--no-threshold-timeout"

set "WAKE_WORD_ARG="
if not "%WAKE_WORD%"=="" set "WAKE_WORD_ARG=--wake-word %WAKE_WORD%"

set "COMMANDS_ARG="
if not "%COMMANDS%"=="" set "COMMANDS_ARG=--commands %COMMANDS%"

if not exist "%SCRIPT_PATH%" (
  echo ERROR: script not found: %SCRIPT_PATH%
  popd
  pause
  exit /b 1
)

if "%DRY_RUN%"=="1" (
  echo DRY RUN ONLY. Command to execute:
  echo %PYTHON_EXE% "%SCRIPT_PATH%" --mode "%MODE%" --cycles "%CYCLES%" %END_AT_ARG% --log-port "%LOG_PORT%" --log-baud "%LOG_BAUD%" --protocol-port "%PROTOCOL_PORT%" --protocol-baud "%PROTOCOL_BAUD%" --audio-dir "%AUDIO_DIR%" --out-dir "%OUT_DIR%" %VOLUME_ARG% %WAKE_WORD_ARG% %COMMANDS_ARG% --threshold-levels "%THRESHOLD_LEVELS%" --protocol-read-seconds "%PROTOCOL_READ_SECONDS%" --post-gap "%POST_GAP%" --wake-gap "%WAKE_GAP%" --gap "%GAP%" --boot-timeout "%BOOT_TIMEOUT%" --post-reboot-grace "%POST_REBOOT_GRACE%" --timeout-wait "%TIMEOUT_WAIT%" %THRESHOLD_TIMEOUT_ARG% --mixed-modes "%MIXED_MODES%" --random-seed "%RANDOM_SEED%"
  popd
  pause
  exit /b 0
)

echo Starting Aopu combo pressure...
echo ModeId=%MODE_ID% Mode=%MODE% Cycles=%CYCLES% EndAt=%END_AT%
echo Log=%LOG_PORT%@%LOG_BAUD% Protocol=%PROTOCOL_PORT%@%PROTOCOL_BAUD% Volume=%VOLUME% SkipVolume=%SKIP_VOLUME%
echo.

%PYTHON_EXE% "%SCRIPT_PATH%" ^
  --mode "%MODE%" ^
  --cycles "%CYCLES%" ^
  %END_AT_ARG% ^
  --log-port "%LOG_PORT%" ^
  --log-baud "%LOG_BAUD%" ^
  --protocol-port "%PROTOCOL_PORT%" ^
  --protocol-baud "%PROTOCOL_BAUD%" ^
  --audio-dir "%AUDIO_DIR%" ^
  --out-dir "%OUT_DIR%" ^
  %VOLUME_ARG% ^
  %WAKE_WORD_ARG% ^
  %COMMANDS_ARG% ^
  --threshold-levels "%THRESHOLD_LEVELS%" ^
  --protocol-read-seconds "%PROTOCOL_READ_SECONDS%" ^
  --post-gap "%POST_GAP%" ^
  --wake-gap "%WAKE_GAP%" ^
  --gap "%GAP%" ^
  --boot-timeout "%BOOT_TIMEOUT%" ^
  --post-reboot-grace "%POST_REBOOT_GRACE%" ^
  --timeout-wait "%TIMEOUT_WAIT%" ^
  %THRESHOLD_TIMEOUT_ARG% ^
  --mixed-modes "%MIXED_MODES%" ^
  --random-seed "%RANDOM_SEED%"

set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo Finished with exit code %EXIT_CODE%.

popd
pause
exit /b %EXIT_CODE%
