param([Parameter(Mandatory = $true)][ValidateSet('dsh','codex','claude','workbuddy-desktop')][string]$HostId)
. (Join-Path $PSScriptRoot 'observer_windows_common.ps1')
Assert-ObserverWindows
$taskName = Get-ObserverTaskName $HostId
Stop-ObserverTask $taskName
if ($null -ne (Find-ObserverTask $taskName)) { Unregister-ScheduledTask -TaskName $taskName -TaskPath '\' -Confirm:$false }
if ($null -ne (Find-ObserverTask $taskName)) { throw 'Selected task removal postcondition failed' }
@{status='ok'; host=$HostId; state_config_credentials_preserved=$true} | ConvertTo-Json -Compress
