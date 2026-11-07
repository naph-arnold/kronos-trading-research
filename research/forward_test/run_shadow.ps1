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

while (-not (Test-Path (Join-Path $state "STOP"))) {
    Add-Content $log "$(Get-Date -Format o) starting runner"
    & $python -m research.phase2d_shadow run *>> $log
    Add-Content $log "$(Get-Date -Format o) runner exited with code $LASTEXITCODE; restarting in 30 s"
    Start-Sleep -Seconds 30
}
Add-Content $log "$(Get-Date -Format o) STOP file present; supervisor exiting"
