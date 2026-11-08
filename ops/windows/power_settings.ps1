# Kronos - power settings for unattended market monitoring (PREPARED; changes only with -Apply / -Revert).
#
# Finding (System log, 2026-11-06..08): this laptop supports only S0 Modern Standby (network connected) and enters it
# after 3 minutes idle on AC ("Reason: Idle Timeout"). In standby the MT5 terminal loses its broker connection and the
# runners stall; XAUUSD reconnect/disconnect times matched standby exit/entry to the second. A process-scoped
# ES_SYSTEM_REQUIRED request did not prevent idle standby here (2026-11-08 05:09 UTC), so the reliable fix without
# keeping the screen on is the power plan itself:
#   * AC "sleep after" (STANDBYIDLE) = never  -> no idle standby while plugged in
#   * AC display timeout (VIDEOIDLE) unchanged -> the screen still turns off after idle
#   * AC lid action = do nothing (optional, -LidDoNothing) -> closing the lid on AC does not sleep
# Battery (DC) settings are not touched. -Apply saves the previous values to power_settings_backup.json; -Revert
# restores them.
param([switch]$Apply, [switch]$Revert, [switch]$LidDoNothing)

$backup = Join-Path $PSScriptRoot "power_settings_backup.json"
function Get-AcIndex([string]$sub, [string]$setting) {
    $line = powercfg /query SCHEME_CURRENT $sub $setting | Select-String "Current AC Power Setting Index"
    if (-not $line) { return $null }
    return [Convert]::ToInt32(($line.ToString() -split "0x")[1].Trim(), 16)
}
$current = [ordered]@{
    scheme = (powercfg /getactivescheme)
    STANDBYIDLE_ac_seconds = Get-AcIndex SUB_SLEEP STANDBYIDLE
    VIDEOIDLE_ac_seconds = Get-AcIndex SUB_VIDEO VIDEOIDLE
    LIDACTION_ac = Get-AcIndex SUB_BUTTONS LIDACTION
}
"Current: $($current | ConvertTo-Json -Compress)"

if ($Revert) {
    if (-not (Test-Path $backup)) { "No backup found; nothing to revert."; return }
    $saved = Get-Content $backup | ConvertFrom-Json
    powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP STANDBYIDLE $saved.STANDBYIDLE_ac_seconds
    if ($null -ne $saved.LIDACTION_ac) { powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION $saved.LIDACTION_ac }
    powercfg /setactive SCHEME_CURRENT
    Remove-Item $backup  # originals restored; the next -Apply records fresh originals
    "Reverted to $($saved | ConvertTo-Json -Compress)"
    return
}
if (-not $Apply) {
    "DRY RUN - would run:"
    "  powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP STANDBYIDLE 0"
    if ($LidDoNothing) { "  powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0" }
    "  powercfg /setactive SCHEME_CURRENT"
    "No changes made. Re-run with -Apply after approval (and -Revert to undo)."
    return
}
if (Test-Path $backup) {
    "Keeping existing backup (original values) at $backup"  # a second -Apply must not overwrite the originals
} else {
    $current | ConvertTo-Json | Set-Content $backup
}
powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP STANDBYIDLE 0
if ($LidDoNothing) { powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0 }
powercfg /setactive SCHEME_CURRENT
"Applied. Backup: $backup"
