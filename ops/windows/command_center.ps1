# Kronos Trading Command Center - Windows launcher (read-only dashboard; never touches the trading runners).
#
#   Double-click "Kronos Command Center.cmd"       -> start (if not running) and open the browser
#   Double-click "Stop Kronos Command Center.cmd"  -> stop the dashboard cleanly
#
#   .\command_center.ps1 [-Action start|stop|status|open|restart|setup|rebuild] [-NoBrowser] [-Port 8765]
#
# Guarantees:
#   * uses the dashboard's own environment (.venv-cc) - never the runners' .venv311;
#   * one instance: a named mutex serialises launcher runs, /api/health identifies a running dashboard, and the
#     server itself refuses a busy port;
#   * stop = graceful: writes a stop request in %LOCALAPPDATA%\KronosCommandCenter, waits for the server to exit,
#     and only then (fallback) ends the process whose PID the dashboard itself reported AND whose command line is
#     the dashboard's - it can never select a runner process (phase2d/phase2e/selective-A command lines are refused);
#   * never starts, stops, restarts or modifies a trading runner, a scheduled task or a STOP/KILL file.
param(
    [ValidateSet("start", "stop", "status", "open", "restart", "setup", "rebuild")][string]$Action = "start",
    [switch]$NoBrowser,
    [int]$Port = 8765,
    [string]$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
)
$ErrorActionPreference = "Stop"
$Url = "http://127.0.0.1:$Port"
$Runtime = Join-Path $env:LOCALAPPDATA "KronosCommandCenter"
$Logs = Join-Path $Runtime "logs"
$Py = Join-Path $Repo ".venv-cc\Scripts\python.exe"
$Web = Join-Path $Repo "command_center\web"
$RunnerPattern = "phase2d_shadow|phase2e_binance_shadow|xau_selective_a_shadow|kronos_supervisor"

function Say([string]$msg, [string]$color = "Gray") { Write-Host "[Kronos Command Center] $msg" -ForegroundColor $color }
function Fail([string]$msg, [int]$code = 1) {
    Say $msg "Red"
    exit $code  # the .cmd wrapper pauses on errors so the message stays visible
}

function Get-Health {
    try {
        $h = Invoke-RestMethod -Uri "$Url/api/health" -TimeoutSec 2 -UseBasicParsing
        if ($h.service -eq "kronos-command-center") { return $h }
    } catch {}
    return $null
}

function Test-PortBusy {
    $c = New-Object Net.Sockets.TcpClient
    try { $c.Connect("127.0.0.1", $Port); return $true } catch { return $false } finally { $c.Dispose() }
}

function Get-DashboardProcess([int]$ProcessId) {
    # Only a process whose command line is the dashboard's, and never anything that looks like a runner.
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if (-not $p) { return $null }
    if ($p.CommandLine -match $RunnerPattern) { throw "Refusing: PID $ProcessId looks like a trading runner." }
    if ($p.CommandLine -notmatch "-m command_center") { return $null }
    return $p
}

function Ensure-Setup {
    if (-not (Test-Path $Py)) {
        Say "Creating the dashboard environment (.venv-cc)..." "Cyan"
        $base = Join-Path $env:APPDATA "uv\python\cpython-3.11-windows-x86_64-none\python.exe"
        if (-not (Test-Path $base)) { $base = (Get-Command python -ErrorAction SilentlyContinue).Source }
        if (-not $base) { Fail "Python 3.11 not found. Install Python 3.11, then run this again." }
        & $base -m venv (Join-Path $Repo ".venv-cc")
        & $Py -m pip install -q -r (Join-Path $Repo "command_center\requirements.txt")
        if ($LASTEXITCODE -ne 0) { Fail "Installing the dashboard packages failed (see messages above)." }
    }
    if (-not (Test-Path (Join-Path $Web "out\index.html"))) { Build-Frontend }
}

function Build-Frontend {
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        Fail "The web interface has not been built and Node.js is not installed. Install Node.js 22 LTS, then run: command_center.ps1 -Action rebuild"
    }
    Say "Building the web interface (one-off, about a minute)..." "Cyan"
    Push-Location $Web
    try {
        if (-not (Test-Path node_modules)) { npm ci --no-audit --no-fund; if ($LASTEXITCODE) { Fail "npm ci failed." } }
        $env:NEXT_TELEMETRY_DISABLED = "1"
        npx next build
        if ($LASTEXITCODE) { Fail "The web build failed (see messages above)." }
    } finally { Pop-Location }
}

function Open-Browser { if (-not $NoBrowser) { Start-Process $Url } }

function Start-Dashboard {
    $h = Get-Health
    if ($h) { Say "Already running (pid $($h.pid), since $($h.started_at)). Opening the browser." "Green"; Open-Browser; return }
    if (Test-PortBusy) { Fail "Port $Port is used by another program (not the Command Center). Close it or use -Port." 3 }
    Ensure-Setup
    New-Item -ItemType Directory -Force $Logs | Out-Null
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $out = Join-Path $Logs "server_$stamp.out.log"
    $err = Join-Path $Logs "server_$stamp.err.log"
    $env:PYTHONPATH = $Repo
    $p = Start-Process -FilePath $Py -ArgumentList @("-m", "command_center", "--port", "$Port") -WorkingDirectory $Repo `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput $out -RedirectStandardError $err
    Say "Starting (launcher pid $($p.Id))..." "Cyan"
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 500
        $h = Get-Health
        if ($h) { Say "Running at $Url (pid $($h.pid)). Read-only; no orders possible." "Green"; Open-Browser; return }
        if ($p.HasExited) { break }
    }
    $detail = if (Test-Path $err) { (Get-Content $err -Tail 8) -join "`n" } else { "" }
    Fail "The dashboard did not start.`n$detail`nLogs: $Logs"
}

function Stop-Dashboard {
    $h = Get-Health
    if (-not $h) { Say "Not running." "Yellow"; return }
    New-Item -ItemType Directory -Force $Runtime | Out-Null
    Set-Content -Path (Join-Path $Runtime "stop.request") -Value (Get-Date -Format o)
    Say "Stopping (pid $($h.pid))..." "Cyan"
    for ($i = 0; $i -lt 20; $i++) { Start-Sleep -Milliseconds 500; if (-not (Get-Health)) { Say "Stopped." "Green"; return } }
    $proc = Get-DashboardProcess -ProcessId ([int]$h.pid)
    if ($proc) {
        Stop-Process -Id $proc.ProcessId -Force
        Say "Graceful stop timed out; ended the dashboard process $($proc.ProcessId) (read-only, nothing to save)." "Yellow"
    } else { Fail "Could not identify the dashboard process safely; nothing was stopped." }
}

$mutex = New-Object System.Threading.Mutex($false, "Local\Kronos.CommandCenter.Launcher")
if (-not $mutex.WaitOne(15000)) { Fail "Another launcher run is in progress." }
try {
    switch ($Action) {
        "start"   { Start-Dashboard }
        "open"    { if (Get-Health) { Open-Browser } else { Start-Dashboard } }
        "stop"    { Stop-Dashboard }
        "restart" { Stop-Dashboard; Start-Dashboard }
        "status"  {
            $h = Get-Health
            if ($h) { Say "Running at $Url - pid $($h.pid), started $($h.started_at), version $($h.version), read-only." "Green" }
            else { Say "Not running." "Yellow" }
        }
        "setup"   { Ensure-Setup; Say "Setup complete." "Green" }
        "rebuild" { Build-Frontend; Say "Web interface rebuilt. Restart the dashboard to serve it." "Green" }
    }
} finally { $mutex.ReleaseMutex() }
