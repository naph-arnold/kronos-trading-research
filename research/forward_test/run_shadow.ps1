# Phase 2D shadow forward test - supervised loop (simulation only; the runner cannot place orders).
# Restarts the runner after any exit until state\phase2d_shadow\STOP exists. Output is appended to
# state\phase2d_shadow\runner.log. Optional: register this script as a Windows scheduled task (operator decision).
param([string]$Repo = (Resolve-Path "$PSScriptRoot\..\..").Path)

$ErrorActionPreference = "Continue"
Set-Location $Repo
$env:EXECUTION_MODE = "demo"
$state = Join-Path $Repo "state\phase2d_shadow"
New-Item -ItemType Directory -Force $state | Out-Null
$log = Join-Path $state "runner.log"
$python = Join-Path $Repo ".venv311\Scripts\python.exe"

# Keep the PC from idle-sleeping while this supervisor runs (ES_CONTINUOUS | ES_SYSTEM_REQUIRED). Scoped to this
# process: Windows drops the request automatically when it exits; no power-plan setting is changed. Does not
# prevent lid-close or manual sleep. The display may still turn off.
Add-Type -Namespace Kronos -Name Power -MemberDefinition @"
[System.Runtime.InteropServices.DllImport("kernel32.dll")]
public static extern uint SetThreadExecutionState(uint esFlags);
"@
$previous = [Kronos.Power]::SetThreadExecutionState([uint32]"0x80000001")
Add-Content $log "$(Get-Date -Format o) keep-awake requested (previous state 0x$('{0:X}' -f $previous))"

while (-not (Test-Path (Join-Path $state "STOP"))) {
    Add-Content $log "$(Get-Date -Format o) starting runner"
    & $python -m research.phase2d_shadow run *>> $log
    Add-Content $log "$(Get-Date -Format o) runner exited with code $LASTEXITCODE; restarting in 30 s"
    Start-Sleep -Seconds 30
}
[Kronos.Power]::SetThreadExecutionState([uint32]"0x80000000") | Out-Null  # release keep-awake
Add-Content $log "$(Get-Date -Format o) STOP file present; supervisor exiting"
