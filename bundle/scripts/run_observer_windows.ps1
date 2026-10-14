param(
    [Parameter(Mandatory = $true)][ValidateSet('dsh','codex','claude','workbuddy-desktop')][string]$HostId,
    [Parameter(Mandatory = $true)][string]$PluginDirectory,
    [Parameter(Mandatory = $true)][string]$PythonExecutable,
    [Parameter(Mandatory = $true)][string]$SourceRoot,
    [Parameter(Mandatory = $true)][string]$StateDirectory,
    [Parameter(Mandatory = $true)][string]$ConfigFile,
    [Parameter(Mandatory = $true)][string]$RuntimeDirectory
)
. (Join-Path $PSScriptRoot 'observer_windows_common.ps1')
Assert-ObserverWindows
$planOutput = Invoke-ObserverNative $PythonExecutable @((Join-Path $PSScriptRoot 'observer_windows.py'),'plan','--host',$HostId,'--install-root',$PluginDirectory,'--python',$PythonExecutable,'--source-root',$SourceRoot,'--state-dir',$StateDirectory,'--config-file',$ConfigFile,'--runtime-dir',$RuntimeDirectory) 30
$plan = $planOutput | ConvertFrom-Json
$info = New-Object System.Diagnostics.ProcessStartInfo
$info.FileName = $plan.python
$info.Arguments = $plan.arguments
$info.UseShellExecute = $false
$process = [System.Diagnostics.Process]::Start($info)
try {
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) { throw "Selected worker exited with native code $($process.ExitCode)" }
} finally { $process.Dispose() }
