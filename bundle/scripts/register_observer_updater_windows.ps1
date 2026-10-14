# Register (or remove) the Observer self-update task for the current user.
# The task runs scripts\observer_update.py every 6 hours and at logon, without a
# window. It only installs releases signed with the key built into the bundle.
param(
    [ValidateSet('install','uninstall','status')][string]$Mode = 'install',
    [string]$PythonExecutable,
    [string]$UpdaterScript,
    [string]$StateDirectory
)
. (Join-Path $PSScriptRoot 'observer_windows_common.ps1')
Assert-ObserverWindows
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$taskName = "AIHub Skill MCP Observer Updater - $sid"
$existing = Find-ObserverTask $taskName
if ($Mode -eq 'status') {
    @{status='ok'; task=$taskName; installed=($null -ne $existing); task_state=$(if ($existing) { [string]$existing.State } else { '' })} | ConvertTo-Json -Compress
    return
}
if ($Mode -eq 'uninstall') {
    if ($null -ne $existing) {
        Stop-ObserverTask $taskName
        Unregister-ScheduledTask -TaskName $taskName -TaskPath '\' -Confirm:$false
    }
    @{status='ok'; task=$taskName; removed=($null -ne $existing)} | ConvertTo-Json -Compress
    return
}
if (-not $PythonExecutable -or -not $UpdaterScript -or -not $StateDirectory) { throw 'install requires -PythonExecutable, -UpdaterScript and -StateDirectory' }
if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) { throw 'Observer Python is missing' }
if (-not (Test-Path -LiteralPath $UpdaterScript -PathType Leaf)) { throw 'Observer updater script is missing' }
New-Item -ItemType Directory -Force -Path $StateDirectory | Out-Null
$execute = Get-ObserverWindowlessPath $PythonExecutable
$arguments = (ConvertTo-ObserverArgument $UpdaterScript) + ' run'
if ($null -ne $existing -and @($existing.Actions).Count -eq 1 -and @($existing.Actions)[0].Execute -eq $execute -and @($existing.Actions)[0].Arguments -eq $arguments -and @($existing.Actions)[0].WorkingDirectory -eq $StateDirectory) {
    # Same definition: leave it alone, so an update that runs this installer is not interrupted.
    @{status='ok'; task=$taskName; changed=$false} | ConvertTo-Json -Compress
    return
}
if ($null -ne $existing -and $env:OBSERVER_UPDATER_CHILD -eq '1') {
    # Re-registering would stop the running update; the next manual install refreshes the task.
    @{status='ok'; task=$taskName; changed=$false; deferred=$true} | ConvertTo-Json -Compress
    return
}
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $execute -Argument $arguments -WorkingDirectory $StateDirectory
$logon = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$logon.Delay = 'PT10M'
$triggers = @(
    $logon,
    (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(15) -RepetitionInterval (New-TimeSpan -Hours 6))
)
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -TaskPath '\' -Action $action -Trigger $triggers -Principal $principal -Settings $settings -Description 'AIHub Observer signed self-update' -Force | Out-Null
if ($null -eq (Find-ObserverTask $taskName)) { throw 'Observer updater task registration failed' }
@{status='ok'; task=$taskName; changed=$true} | ConvertTo-Json -Compress
