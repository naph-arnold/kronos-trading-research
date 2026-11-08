# Kronos shadow-runner supervisor (Windows). One supervisor per market; simulation only (runners cannot place orders).
#
# Safety properties:
#   * Single instance per market: a named mutex (Local\Kronos.Shadow.<Name>) plus a scan for any process already
#     running this market's runner module with this state directory (covers supervisors started before this script,
#     manual runs and scheduled-task restarts). If either is found, this supervisor exits without starting anything.
#   * STOP is authoritative: checked before every (re)start. While <StateDir>\STOP exists nothing is started, and this
#     script never creates, renames or deletes STOP or KILL. KILL is enforced by the runner's circuit breaker (no new
#     simulated entries); restarts cannot bypass it because the breaker reads the file on every decision.
#   * Keep-awake is scoped to this process (SetThreadExecutionState) and released on exit; no power-plan change.
param(
    [Parameter(Mandatory = $true)][ValidateSet("xauusd", "btcusdt")][string]$Name,
    [Parameter(Mandatory = $true)][string]$Repo,
    [Parameter(Mandatory = $true)][string]$Module,       # research.phase2d_shadow | research.phase2e_binance_shadow
    [Parameter(Mandatory = $true)][string]$StateDir,
    [string]$Python = "",
    [ValidateSet("none", "system", "display")][string]$KeepAwake = "system",
    [int]$RestartDelaySeconds = 30
)

$ErrorActionPreference = "Continue"
Set-Location $Repo
if (-not $Python) { $Python = Join-Path $Repo ".venv311\Scripts\python.exe" }
$env:EXECUTION_MODE = "demo"
New-Item -ItemType Directory -Force $StateDir | Out-Null
$log = Join-Path $StateDir "supervisor.log"
$stop = Join-Path $StateDir "STOP"
function Write-Log([string]$message) { Add-Content $log "$(Get-Date -Format o) [pid $PID] $message" }

# -- single instance --------------------------------------------------------------------------------------------
$mutex = New-Object System.Threading.Mutex($false, "Local\Kronos.Shadow.$Name")
if (-not $mutex.WaitOne(0)) { Write-Log "another $Name supervisor holds the mutex; exiting"; exit 0 }
# One runner per market module on this machine (production state is single-writer). Matches runners started by any
# supervisor version, by hand or by a scheduled task, with or without --state-dir.
$pattern = [regex]::Escape("-m $Module run")
$existing = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match $pattern }
if ($existing) {
    Write-Log "runner already running (pid $(@($existing.ProcessId) -join ',')); not starting a duplicate; exiting"
    $mutex.ReleaseMutex(); exit 0
}

# -- keep-awake ---------------------------------------------------------------------------------------------------
Add-Type -Namespace KronosOps -Name Power -MemberDefinition @"
[System.Runtime.InteropServices.DllImport("kernel32.dll")]
public static extern uint SetThreadExecutionState(uint esFlags);
"@
$flags = @{ none = [uint32]"0x80000000"; system = [uint32]"0x80000001"; display = [uint32]"0x80000003" }[$KeepAwake]
[KronosOps.Power]::SetThreadExecutionState($flags) | Out-Null
Write-Log "supervisor started (keep-awake=$KeepAwake, python=$Python, module=$Module)"

try {
    while ($true) {
        if (Test-Path $stop) { Write-Log "STOP present; not (re)starting the runner"; break }
        Write-Log "starting runner"
        & $Python -m $Module run --state-dir $StateDir *>> (Join-Path $StateDir "runner.log")
        Write-Log "runner exited with code $LASTEXITCODE"
        if (Test-Path $stop) { Write-Log "STOP present after exit; supervisor exiting"; break }
        Start-Sleep -Seconds $RestartDelaySeconds
    }
}
finally {
    [KronosOps.Power]::SetThreadExecutionState([uint32]"0x80000000") | Out-Null
    Write-Log "supervisor exiting"
    $mutex.ReleaseMutex()
}
