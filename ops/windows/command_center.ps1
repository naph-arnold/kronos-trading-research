# Kronos Trading Command Center V1 - start the read-only dashboard (foreground; Ctrl+C stops it).
#
#   .\ops\windows\command_center.ps1            # serve http://127.0.0.1:8765 (builds the frontend if missing)
#   .\ops\windows\command_center.ps1 -Rebuild   # rebuild the static frontend first (needs Node.js)
#   .\ops\windows\command_center.ps1 -Setup     # create .venv-cc and install the backend packages
#
# Independent of the trading runners: its own Python venv (.venv-cc), its own process, read-only SQLite
# connections, GET-only API, bound to 127.0.0.1. It is NOT a scheduled task or service; nothing starts it
# automatically. Stopping it has no effect on the runners.
param(
    [switch]$Setup,
    [switch]$Rebuild,
    [string]$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [int]$Port = 8765
)
$ErrorActionPreference = "Stop"
Set-Location $Repo
$py = Join-Path $Repo ".venv-cc\Scripts\python.exe"
if ($Setup -or -not (Test-Path $py)) {
    $base = "C:\Users\Admin\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe"
    if (-not (Test-Path $base)) { $base = "python" }
    & $base -m venv .venv-cc
    & $py -m pip install -q -r command_center\requirements.txt
}
$web = Join-Path $Repo "command_center\web"
if ($Rebuild -or -not (Test-Path (Join-Path $web "out\index.html"))) {
    Push-Location $web
    try {
        if (-not (Test-Path node_modules)) { npm ci --no-audit --no-fund }
        $env:NEXT_TELEMETRY_DISABLED = "1"
        npx next build
    } finally { Pop-Location }
}
$env:PYTHONPATH = $Repo
"Kronos Command Center (read-only): http://127.0.0.1:$Port"
& $py -m command_center --port $Port
