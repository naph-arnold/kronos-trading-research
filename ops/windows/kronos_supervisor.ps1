# Kronos shadow-runner supervisor (Windows). One supervisor per market; simulation only (runners cannot place orders).
#
# Safety properties:
#   * Single supervisor per market: named mutex Local\Kronos.Shadow.<Name>; a second supervisor exits immediately.
#   * Never two runners: before every start the supervisor scans for ANY process already running this market's runner
#     module ("-m <Module> run"), whoever started it (an older supervisor, a person, a task). If one exists, it is
#     ADOPTED: the supervisor waits for it to exit instead of starting another. After an adopted runner exits, the
#     supervisor waits a grace period (default 90 s, longer than the 30 s restart delay of older supervisors) and
#     scans again before starting anything.
#   * STOP is authoritative: checked before every start and while adopting. While <StateDir>\STOP exists nothing is
#     started. This script never creates, renames or deletes STOP or KILL. KILL is enforced by the runner's circuit
#     breaker on every decision, so restarts cannot bypass it.
#   * Keep-awake is scoped to this process (SetThreadExecutionState) and released on exit; no power-plan change.
param(
    [Parameter(Mandatory = $true)][ValidateSet("xauusd", "btcusdt", "xauselectivea", "test")][string]$Name,
    [Parameter(Mandatory = $true)][string]$Repo,
    [Parameter(Mandatory = $true)][string]$Module,       # research.phase2d_shadow | research.phase2e_binance_shadow | research.xau_selective_a_shadow
    [Parameter(Mandatory = $true)][string]$StateDir,
    [string]$Python = "",
    [ValidateSet("none", "system", "display")][string]$KeepAwake = "system",
    [int]$RestartDelaySeconds = 30,
    [int]$AdoptGraceSeconds = 90
)

$ErrorActionPreference = "Continue"
Set-Location $Repo
if (-not $Python) { $Python = Join-Path $Repo ".venv311\Scripts\python.exe" }
$env:EXECUTION_MODE = "demo"
New-Item -ItemType Directory -Force $StateDir | Out-Null
$log = Join-Path $StateDir "supervisor.log"
$stop = Join-Path $StateDir "STOP"
function Write-Log([string]$message) { Add-Content $log "$(Get-Date -Format o) [pid $PID] $message" }

# -- single supervisor -------------------------------------------------------------------------------------------
$mutex = New-Object System.Threading.Mutex($false, "Local\Kronos.Shadow.$Name")
if (-not $mutex.WaitOne(0)) { Write-Log "another $Name supervisor holds the mutex; exiting"; exit 0 }

# One runner per market module on this machine (production state is single-writer).
$pattern = [regex]::Escape("-m $Module run")
function Get-Runners { @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match $pattern }) }

# Wait while any runner for this module exists. Returns $true if STOP appeared while waiting.
function Wait-Adopted {
    $adopted = $false
    while ($true) {
        $running = Get-Runners
        if ($running.Count -eq 0) { break }
        if (-not $adopted) { Write-Log "adopting running runner (pid $(($running.ProcessId) -join ',')); not starting another"; $adopted = $true }
        if (Test-Path $stop) { Write-Log "STOP present while adopting; supervisor will not restart after the runner exits" }
        Start-Sleep -Seconds 5
    }
    if ($adopted) { Write-Log "adopted runner exited" }
    return $adopted
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
        if (Wait-Adopted) {
            Start-Sleep -Seconds $AdoptGraceSeconds  # let an older supervisor restart its own runner first
            continue                                 # re-check STOP and scan again
        }
        if (Test-Path $stop) { Write-Log "STOP present; not (re)starting the runner"; break }
        # The runner writes its output straight to files (no pipe through this process), so it survives if this
        # supervisor is terminated; the next supervisor adopts it. One log pair per start under <StateDir>\logs.
        $logs = Join-Path $StateDir "logs"; New-Item -ItemType Directory -Force $logs | Out-Null
        $stamp = Get-Date -Format "yyyyMMddTHHmmss"
        Write-Log "starting runner"
        $proc = Start-Process -FilePath $Python -ArgumentList @("-m", $Module, "run", "--state-dir", "`"$StateDir`"") `
            -WorkingDirectory $Repo -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $logs "runner_$stamp.out.log") -RedirectStandardError (Join-Path $logs "runner_$stamp.err.log")
        $null = $proc.Handle  # cache the handle so ExitCode is available after WaitForExit
        Write-Log "started runner pid $($proc.Id) (logs\runner_$stamp.*)"
        $proc.WaitForExit()
        Write-Log "runner exited with code $($proc.ExitCode)"
        if (Test-Path $stop) { Write-Log "STOP present after exit; supervisor exiting"; break }
        Start-Sleep -Seconds $RestartDelaySeconds
    }
}
finally {
    [KronosOps.Power]::SetThreadExecutionState([uint32]"0x80000000") | Out-Null
    Write-Log "supervisor exiting"
    $mutex.ReleaseMutex()
}
