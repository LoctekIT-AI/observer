param(
    [Parameter(Mandatory = $true)][ValidateSet('dsh','codex','claude','workbuddy-desktop')][string]$HostId,
    [ValidateSet('install','status','uninstall','snapshot','restore')][string]$Mode = 'install',
    [Parameter(Mandatory = $true)][string]$PluginDirectory,
    [Parameter(Mandatory = $true)][string]$PythonExecutable,
    [Parameter(Mandatory = $true)][string]$SourceRoot,
    [Parameter(Mandatory = $true)][string]$StateDirectory,
    [Parameter(Mandatory = $true)][string]$ConfigFile,
    [Parameter(Mandatory = $true)][string]$RuntimeDirectory,
    [string]$TaskXml = '',
    [ValidateSet('True','False')][string]$WasRunning = 'False'
)
. (Join-Path $PSScriptRoot 'observer_windows_common.ps1')
Assert-ObserverWindows
$taskName = Get-ObserverTaskName $HostId
$existing = Find-ObserverTask $taskName
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
if ($null -ne $existing) {
    $principalId = $existing.Principal.UserId
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    # Task Scheduler may report the principal as DOMAIN\user, just user, or a SID.
    $principalSid = $null
    try { $principalSid = (New-Object System.Security.Principal.NTAccount($principalId)).Translate([System.Security.Principal.SecurityIdentifier]).Value } catch { $principalSid = $null }
    if ($principalId -ne $currentUser -and $principalId -ne $sid -and $principalSid -ne $sid) { throw 'Selected task is owned by another principal' }
}
if ($Mode -eq 'snapshot') {
    $xml = if ($null -ne $existing) { Export-ScheduledTask -TaskName $taskName -TaskPath '\' } else { '' }
    @{status='ok'; xml=$xml; running=($null -ne $existing -and $existing.State -eq 'Running')} | ConvertTo-Json -Compress
    return
}
if ($Mode -eq 'uninstall') {
    Stop-ObserverTask $taskName
    if ($null -ne (Find-ObserverTask $taskName)) { Unregister-ScheduledTask -TaskName $taskName -TaskPath '\' -Confirm:$false }
    if ($null -ne (Find-ObserverTask $taskName)) { throw 'Selected task removal postcondition failed' }
    @{status='ok'; host=$HostId; state_config_credentials_preserved=$true} | ConvertTo-Json -Compress
    return
}
if ($Mode -eq 'restore') {
    Stop-ObserverTask $taskName
    if ($TaskXml) {
        Register-ScheduledTask -TaskName $taskName -TaskPath '\' -Xml $TaskXml -Force | Out-Null
        if ($WasRunning -eq 'True') { Start-ScheduledTask -TaskName $taskName -TaskPath '\' }
        if ($null -eq (Find-ObserverTask $taskName)) { throw 'Selected prior task restoration failed' }
    } elseif ($null -ne (Find-ObserverTask $taskName)) {
        Unregister-ScheduledTask -TaskName $taskName -TaskPath '\' -Confirm:$false
    }
    @{status='ok'; restored=[bool]$TaskXml} | ConvertTo-Json -Compress
    return
}
$helper = Join-Path $PSScriptRoot 'observer_windows.py'
$planOutput = Invoke-ObserverNative $PythonExecutable @($helper,'plan','--host',$HostId,'--install-root',$PluginDirectory,'--python',$PythonExecutable,'--source-root',$SourceRoot,'--state-dir',$StateDirectory,'--config-file',$ConfigFile,'--runtime-dir',$RuntimeDirectory) 30
$plan = $planOutput | ConvertFrom-Json
if ($Mode -eq 'status') {
    if ($null -eq $existing) { throw 'Selected Observer task is not installed' }
    $heartbeat = Assert-ObserverHeartbeat $taskName $StateDirectory ([datetimeoffset]::Now.AddSeconds(-30)) $plan
    $info = Get-ScheduledTaskInfo -TaskName $taskName -TaskPath '\'
    @{status='ok'; host=$HostId; task=$taskName; task_state=[string]$existing.State; last_task_result=$info.LastTaskResult; heartbeat=$heartbeat} | ConvertTo-Json -Depth 8 -Compress
    return
}
# --check verifies the real native source and protected config without consuming events.
Invoke-ObserverNative $plan.python (@($plan.argv) + @('--check')) 30 | Out-Null
$oldXml = if ($null -ne $existing) { Export-ScheduledTask -TaskName $taskName -TaskPath '\' } else { '' }
$oldRunning = $null -ne $existing -and $existing.State -eq 'Running'
try {
    Stop-ObserverTask $taskName
    # Run from the state folder, not the bundle: a working directory inside the
    # bundle would block activating a newer bundle on Windows.
    New-Item -ItemType Directory -Force -Path $StateDirectory | Out-Null
    # pythonw.exe: the worker runs in the background without a console window.
    $action = New-ScheduledTaskAction -Execute (Get-ObserverWindowlessPath $plan.python) -Argument $plan.arguments -WorkingDirectory $StateDirectory
    $triggers = @(
        (New-ScheduledTaskTrigger -AtLogOn -User $currentUser),
        (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1))
    )
    $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([timespan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $taskName -TaskPath '\' -Action $action -Trigger $triggers -Principal $principal -Settings $settings -Description "AIHub selected $HostId Observer worker" -Force | Out-Null
    $started = [datetimeoffset]::Now
    Start-ScheduledTask -TaskName $taskName -TaskPath '\'
    $heartbeat = Assert-ObserverHeartbeat $taskName $StateDirectory $started $plan
    @{status='ok'; host=$HostId; task=$taskName; enabled=$true; heartbeat=$heartbeat} | ConvertTo-Json -Depth 8 -Compress
} catch {
    $nativeFailure = $_
    Stop-ObserverTask $taskName
    if ($oldXml) {
        Register-ScheduledTask -TaskName $taskName -TaskPath '\' -Xml $oldXml -Force | Out-Null
        if ($oldRunning) { Start-ScheduledTask -TaskName $taskName -TaskPath '\' }
    } elseif ($null -ne (Find-ObserverTask $taskName)) {
        Unregister-ScheduledTask -TaskName $taskName -TaskPath '\' -Confirm:$false
    }
    throw $nativeFailure
}
