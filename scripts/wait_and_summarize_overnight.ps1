param(
    [Parameter(Mandatory = $true)]
    [string]$RunDir,

    [string]$TargetTime = "2026-06-25 08:02:00",

    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Continue"
$runPath = Resolve-Path -LiteralPath $RunDir
$logPath = Join-Path $runPath "morning_summary_runner.log"
$summaryJson = Join-Path $runPath "overnight_pressure.summary.json"
$scriptPath = Join-Path (Split-Path -Parent $PSCommandPath) "summarize_pressure_results.py"

function Write-RunnerLog {
    param([string]$Message)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
}

Write-RunnerLog "runner started; target=$TargetTime; run_dir=$runPath"

$target = [datetime]::Parse($TargetTime)
while ((Get-Date) -lt $target) {
    $remain = [int][Math]::Min(60, [Math]::Max(1, ($target - (Get-Date)).TotalSeconds))
    Start-Sleep -Seconds $remain
}

Write-RunnerLog "target time reached; waiting for pressure summary to finish"

for ($i = 0; $i -lt 20; $i++) {
    try {
        if (Test-Path -LiteralPath $summaryJson) {
            $summary = Get-Content -LiteralPath $summaryJson -Encoding UTF8 -Raw | ConvertFrom-Json
            Write-RunnerLog "summary status=$($summary.status); total_cases=$($summary.total_cases)"
            if ($summary.status -in @("finished", "stopped")) {
                break
            }
        } else {
            Write-RunnerLog "summary json not found yet"
        }
    } catch {
        Write-RunnerLog "summary read failed: $($_.Exception.Message)"
    }
    Start-Sleep -Seconds 30
}

Write-RunnerLog "generating markdown report"
$env:PYTHONIOENCODING = "utf-8"
& $PythonExe $scriptPath --run-dir $runPath *> (Join-Path $runPath "morning_summary_python.log")
Write-RunnerLog "report generation command exited with code $LASTEXITCODE"
