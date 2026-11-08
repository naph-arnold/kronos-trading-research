# Kronos shadow runners - Windows Task Scheduler configuration (PREPARED; applies only with -Apply).
#
#   .\scheduled_tasks.ps1                 # dry run: prints the task definitions, changes nothing
#   .\scheduled_tasks.ps1 -Apply          # registers/updates the tasks (persistent system change: needs approval)
#   .\scheduled_tasks.ps1 -Remove         # unregisters the tasks
#
# Tasks (current user, "run only when user is logged on" - the MT5 terminal runs in the user session):
#   Kronos Shadow XAUUSD   at log-on + every 15 minutes (re-check)   -> ops\windows\kronos_supervisor.ps1 -Name xauusd
#   Kronos Shadow BTCUSDT  at log-on + every 15 minutes (re-check)   -> ops\windows\kronos_supervisor.ps1 -Name btcusdt
#   Kronos Daily Health    daily 00:15 local                         -> research\dual_health_report.py (read-only)
# Duplicate protection: MultipleInstances=IgnoreNew (scheduler) + mutex + running-runner scan (supervisor). The
# 15-minute repetition only restores a supervisor that has died; it never starts a second one and never overrides STOP.
param(
    [switch]$Apply,
    [switch]$Remove,
    [string]$GoldRepo = "C:\Users\Admin\Projects\kronos-trading-research",
    [string]$CryptoRepo = "C:\Users\Admin\Projects\kronos-binance-shadow",
    [string]$Python = "C:\Users\Admin\Projects\kronos-trading-research\.venv311\Scripts\python.exe",
    [ValidateSet("none", "system", "display")][string]$KeepAwake = "system"
)

$user = "$env:USERDOMAIN\$env:USERNAME"
$supervisor = Join-Path $GoldRepo "ops\windows\kronos_supervisor.ps1"
$definitions = @(
    @{ Name = "Kronos Shadow XAUUSD"; Args = "-Name xauusd -Repo `"$GoldRepo`" -Module research.phase2d_shadow -StateDir `"$GoldRepo\state\phase2d_shadow`" -Python `"$Python`" -KeepAwake $KeepAwake"; WorkDir = $GoldRepo },
    @{ Name = "Kronos Shadow BTCUSDT"; Args = "-Name btcusdt -Repo `"$CryptoRepo`" -Module research.phase2e_binance_shadow -StateDir `"$CryptoRepo\state\phase2e_binance_shadow`" -Python `"$Python`" -KeepAwake $KeepAwake"; WorkDir = $CryptoRepo }
)

if ($Remove) {
    foreach ($d in $definitions + @(@{ Name = "Kronos Daily Health" })) {
        Unregister-ScheduledTask -TaskName $d.Name -Confirm:$false -ErrorAction SilentlyContinue
        "removed: $($d.Name)"
    }
    return
}

$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$tasks = @()
foreach ($d in $definitions) {
    $logon = New-ScheduledTaskTrigger -AtLogOn -User $user
    $repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 15)
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory $d.WorkDir `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$supervisor`" $($d.Args)"
    $tasks += @{ Name = $d.Name; Task = New-ScheduledTask -Action $action -Trigger @($logon, $repeat) -Settings $settings -Principal $principal `
        -Description "Kronos read-only shadow runner supervisor (simulation only; no orders). STOP file in the state directory prevents starts." }
}
$healthAction = New-ScheduledTaskAction -Execute $Python -WorkingDirectory $GoldRepo -Argument "-m research.dual_health_report"
$tasks += @{ Name = "Kronos Daily Health"; Task = New-ScheduledTask -Action $healthAction -Trigger (New-ScheduledTaskTrigger -Daily -At "00:15") `
    -Settings (New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries) -Principal $principal `
    -Description "Kronos unified read-only daily health report (XAUUSD + BTC/USDT)." }

foreach ($t in $tasks) {
    if ($Apply) {
        Register-ScheduledTask -TaskName $t.Name -InputObject $t.Task -Force | Out-Null
        "registered: $($t.Name)"
    } else {
        "---- DRY RUN (not registered): $($t.Name)"
        ($t.Task | Export-ScheduledTask)
    }
}
if (-not $Apply) { "No changes made. Re-run with -Apply after approval." }
