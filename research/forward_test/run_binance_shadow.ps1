# Phase 2E BTC/USDT shadow runner - supervised loop (public market data only; simulation only; no orders).
# Independent of the XAUUSD supervisor (separate process, state directory, breaker, KILL and STOP files).
# Restarts the runner after any exit until <state>\STOP exists; output appended to <state>\runner.log.
param(
    [string]$Repo = (Resolve-Path "$PSScriptRoot\..\..").Path,
    [string]$Python = ""
)

$ErrorActionPreference = "Continue"
Set-Location $Repo
if (-not $Python) { $Python = Join-Path $Repo ".venv311\Scripts\python.exe" }
$env:EXECUTION_MODE = "demo"
$state = Join-Path $Repo "state\phase2e_binance_shadow"
New-Item -ItemType Directory -Force $state | Out-Null
$log = Join-Path $state "runner.log"

# Process-scoped keep-awake (ES_CONTINUOUS | ES_SYSTEM_REQUIRED); released automatically on exit. No power-plan change.
Add-Type -Namespace Kronos -Name PowerBtc -MemberDefinition @"
[System.Runtime.InteropServices.DllImport("kernel32.dll")]
public static extern uint SetThreadExecutionState(uint esFlags);
"@
[Kronos.PowerBtc]::SetThreadExecutionState([uint32]"0x80000001") | Out-Null
Add-Content $log "$(Get-Date -Format o) keep-awake requested; python=$Python"

while (-not (Test-Path (Join-Path $state "STOP"))) {
    Add-Content $log "$(Get-Date -Format o) starting runner"
    & $Python -m research.phase2e_binance_shadow run --state-dir $state *>> $log
    Add-Content $log "$(Get-Date -Format o) runner exited with code $LASTEXITCODE; restarting in 30 s"
    Start-Sleep -Seconds 30
}
[Kronos.PowerBtc]::SetThreadExecutionState([uint32]"0x80000000") | Out-Null
Add-Content $log "$(Get-Date -Format o) STOP file present; supervisor exiting"
