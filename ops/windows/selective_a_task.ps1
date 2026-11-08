# Kronos XAUUSD Selective Strategy A shadow runner - Task Scheduler entry (PREPARED; DRY RUN unless -Apply).
#
#   .\selective_a_task.ps1            # dry run: prints the task definition and the pre-launch checks, changes nothing
#   .\selective_a_task.ps1 -Apply     # registers the task (persistent system change: needs SEPARATE launch approval)
#   .\selective_a_task.ps1 -Remove    # unregisters it
#
# Third runner, independent of the two running experiments:
#   * runs from its OWN checkout (default C:\Users\Admin\Projects\kronos-selective-a-shadow), never from the gold
#     (kronos-trading-research) or crypto (kronos-binance-shadow) checkouts used by the running runners;
#   * its own state directory <Repo>\state\xau_selective_a_shadow, STOP/KILL files, breaker and daily ledger;
#   * started by the shared supervisor (kronos_supervisor.ps1 -Name xauselectivea): named mutex, running-runner
#     scan/adopt, STOP authoritative; the runner itself also holds <state>\runner.lock (one process per state);
#   * read-only MT5 (allowlist proxy, demo account enforced); simulation only - no orders can be placed.
# -Apply refuses unless the checkout exists, its config says "launch_approved": true (a reviewed commit) and the
# pre-registration hash in the config matches the file.
param(
    [switch]$Apply,
    [switch]$Remove,
    [string]$Repo = "C:\Users\Admin\Projects\kronos-selective-a-shadow",
    [string]$Python = "C:\Users\Admin\Projects\kronos-trading-research\.venv311\Scripts\python.exe",
    [ValidateSet("none", "system", "display")][string]$KeepAwake = "system"
)

$taskName = "Kronos Shadow XAUUSD Selective A"
if ($Remove) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    "removed: $taskName"
    return
}

$problems = @()
$config = Join-Path $Repo "research\configs\xau_selective_a_shadow.json"
if (-not (Test-Path $config)) {
    $problems += "checkout/config missing: $config (create the dedicated worktree first)"
} else {
    $cfg = Get-Content $config -Raw | ConvertFrom-Json
    if ($cfg.launch_approved -ne $true) { $problems += "config launch_approved is not true (launch not approved)" }
    if ($cfg.state_dir -ne "state/xau_selective_a_shadow") { $problems += "unexpected state_dir $($cfg.state_dir)" }
    $prereg = Join-Path $Repo $cfg.preregistration
    if (Test-Path $prereg) {
        $text = [IO.File]::ReadAllText($prereg).Replace("`r`n", "`n")
        $sha = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($text))).Replace("-", "").ToLower()
        if ($sha -ne $cfg.preregistration_sha256) { $problems += "pre-registration hash mismatch (frozen file changed?)" }
    } else { $problems += "pre-registration file missing" }
}
foreach ($p in @("C:\Users\Admin\Projects\kronos-trading-research", "C:\Users\Admin\Projects\kronos-binance-shadow")) {
    if ((Resolve-Path $Repo -ErrorAction SilentlyContinue).Path -eq $p) { $problems += "Repo must not be a running runner's checkout ($p)" }
}

$user = "$env:USERDOMAIN\$env:USERNAME"
$supervisor = Join-Path $Repo "ops\windows\kronos_supervisor.ps1"
$state = Join-Path $Repo "state\xau_selective_a_shadow"
$args_ = "-Name xauselectivea -Repo `"$Repo`" -Module research.xau_selective_a_shadow -StateDir `"$state`" -Python `"$Python`" -KeepAwake $KeepAwake"
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$logon = New-ScheduledTaskTrigger -AtLogOn -User $user
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 15)
$action = New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory $Repo `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$supervisor`" $args_"
$task = New-ScheduledTask -Action $action -Trigger @($logon, $repeat) -Settings $settings -Principal $principal `
    -Description "Kronos XAUUSD Selective Strategy A read-only shadow runner supervisor (simulation only; no orders). STOP file in the state directory prevents starts."

if ($Apply) {
    if ($problems.Count -gt 0) { throw "Refusing to register ${taskName}: $($problems -join '; ')" }
    Register-ScheduledTask -TaskName $taskName -InputObject $task -Force | Out-Null
    "registered: $taskName"
} else {
    "---- DRY RUN (not registered): $taskName"
    ($task | Export-ScheduledTask)
    if ($problems.Count -gt 0) { "Pre-launch checks NOT met: $($problems -join '; ')" } else { "Pre-launch checks met." }
    "No changes made. Re-run with -Apply only after the launch is approved."
}
