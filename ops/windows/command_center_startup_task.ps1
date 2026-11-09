# Kronos Command Center - OPTIONAL start-at-logon task (PREPARED; DRY RUN unless -Apply).
#
#   .\command_center_startup_task.ps1           # prints the task definition; changes nothing
#   .\command_center_startup_task.ps1 -Apply    # registers it (persistent change: needs the operator's approval)
#   .\command_center_startup_task.ps1 -Remove   # unregisters it
#
# The task only runs the launcher's "start -NoBrowser" at logon (30 s delay). It is independent of the runner tasks
# ("Kronos Shadow XAUUSD", "Kronos Shadow BTCUSDT") and never modifies them. Refuses to overwrite an existing task.
param(
    [switch]$Apply,
    [switch]$Remove,
    [string]$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
)
$taskName = "Kronos Command Center (dashboard)"
if ($Remove) { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue; "removed: $taskName"; return }
$user = "$env:USERDOMAIN\$env:USERNAME"
$launcher = Join-Path $Repo "ops\windows\command_center.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory $Repo `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcher`" -Action start -NoBrowser"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$trigger.Delay = "PT30S"
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "Starts the read-only Kronos Command Center at logon (127.0.0.1:8765). Does not touch trading runners."
if ($Apply) {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) { throw "$taskName already exists; not overwriting." }
    Register-ScheduledTask -TaskName $taskName -InputObject $task | Out-Null
    "registered: $taskName"
} else {
    "---- DRY RUN (not registered): $taskName"
    $task | Export-ScheduledTask
    "No changes made. Re-run with -Apply only if you want the dashboard to start at logon."
}
