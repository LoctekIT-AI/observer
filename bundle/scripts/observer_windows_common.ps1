Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-ObserverWindows {
    if ($env:OS -ne 'Windows_NT') { throw 'Windows native Observer lifecycle is unavailable on this platform' }
}

function ConvertTo-ObserverPsLiteral([string]$Value) {
    return "'" + $Value.Replace("'", "''") + "'"
}

function Invoke-ObserverCleanModuleRelaunch([string]$ScriptPath, $Bound) {
    # Windows PowerShell 5.1 started from PowerShell 7 (as some AI clients do)
    # inherits PowerShell 7 module paths, and built-in modules such as
    # ScheduledTasks, Archive or Get-FileHash then fail to load. Run the script
    # once more in a child process with the standard Windows PowerShell path.
    if ($PSVersionTable.PSEdition -ne 'Desktop' -or $env:OBSERVER_CLEAN_MODULE_PATH -eq '1') { return $false }
    if ($env:PSModulePath -notmatch '\\PowerShell\\7|\\Documents\\PowerShell\\Modules|\\Program Files\\PowerShell\\Modules') { return $false }
    $machine = [Environment]::GetEnvironmentVariable('PSModulePath', 'Machine')
    $documents = [Environment]::GetFolderPath('MyDocuments')
    if ($documents) { $env:PSModulePath = (Join-Path $documents 'WindowsPowerShell\Modules') + ';' + $machine } else { $env:PSModulePath = $machine }
    $env:OBSERVER_CLEAN_MODULE_PATH = '1'
    $parts = @('&', (ConvertTo-ObserverPsLiteral $ScriptPath))
    foreach ($key in @($Bound.Keys)) {
        $value = $Bound[$key]
        if ($value -is [System.Management.Automation.SwitchParameter]) {
            if ($value.IsPresent) { $parts += "-$key" }
        } elseif ($value -is [array]) {
            if (@($value).Count -gt 0) { $parts += "-$key"; $parts += ((@($value) | ForEach-Object { ConvertTo-ObserverPsLiteral ([string]$_) }) -join ',') }
        } else {
            $parts += "-$key"; $parts += (ConvertTo-ObserverPsLiteral ([string]$value))
        }
    }
    $command = '$global:LASTEXITCODE = 0; ' + ($parts -join ' ') + '; exit $LASTEXITCODE'
    $encoded = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($command))
    # Show the child's output directly; only the boolean is returned to the caller.
    & (Join-Path $PSHOME 'powershell.exe') -NoProfile -ExecutionPolicy Bypass -EncodedCommand $encoded | Out-Host
    return $true
}

function Resolve-ObserverInputPath([string]$Value) {
    if ($Value -notmatch '^(?:[A-Za-z]:\\|\\\\[^\\]+\\[^\\]+(?:\\|$))' -or $Value.StartsWith('\\?\') -or $Value.StartsWith('\\.\') -or $Value.Contains('"') -or $Value.Contains([char]0) -or $Value.Contains("`n") -or $Value.Contains("`r")) { throw 'Observer inputs require explicit supported absolute paths' }
    $full = [System.IO.Path]::GetFullPath($Value)
    $candidate = $full
    while ($candidate) {
        if (Test-Path -LiteralPath $candidate) {
            $item = Get-Item -LiteralPath $candidate -Force
            # PS5.1 has no unified link canonicalization API; reject reparse aliases and require explicit real paths to avoid out-of-bounds deletes.
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Observer input contains a link or junction; use its explicit canonical path' }
        }
        $candidate = [System.IO.Path]::GetDirectoryName($candidate)
    }
    return $full.TrimEnd('\') + '\'
}

function Assert-ObserverLayout([string]$Bundle, [string[]]$ProtectedPaths) {
    $bundlePath = Resolve-ObserverInputPath $Bundle
    foreach ($value in $ProtectedPaths) {
        if ($null -eq $value -or $value -eq '') { continue }
        $otherPath = Resolve-ObserverInputPath $value
        if ($bundlePath.StartsWith($otherPath,[System.StringComparison]::OrdinalIgnoreCase) -or $otherPath.StartsWith($bundlePath,[System.StringComparison]::OrdinalIgnoreCase)) { throw 'Bundle must not overlap any protected customer input' }
    }
}

function ConvertTo-ObserverArgument([string]$Value) {
    if ($Value.Contains([char]0) -or $Value.Contains("`r") -or $Value.Contains("`n")) { throw 'Native argument must be single-line without NUL' }
    # Follow CommandLineToArgvW backslash rules; keep spaces, quotes and trailing path separators.
    $quoted = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $quoted = [regex]::Replace($quoted, '(\\+)$', '$1$1')
    return '"' + $quoted + '"'
}

function Invoke-ObserverNative([string]$Executable, [string[]]$Arguments, [int]$TimeoutSeconds = 180) {
    if (-not [System.IO.Path]::IsPathRooted($Executable) -or -not (Test-Path -LiteralPath $Executable -PathType Leaf)) { throw "Native executable must be an existing absolute file: $Executable" }
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $Executable
    $info.Arguments = (($Arguments | ForEach-Object { ConvertTo-ObserverArgument $_ }) -join ' ')
    $info.UseShellExecute = $false
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.CreateNoWindow = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $info
    try {
        if (-not $process.Start()) { throw "Native process did not start: $Executable" }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            & "$env:SystemRoot\System32\taskkill.exe" /PID $process.Id /T /F | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "Native timeout and process-tree termination failed: $Executable" }
            throw "Native process timed out after $TimeoutSeconds seconds: $Executable"
        }
        if (-not $stdout.Wait(5000) -or -not $stderr.Wait(5000)) { throw "Native output streams did not close within finite wait: $Executable" }
        $output = $stdout.GetAwaiter().GetResult()
        $errorOutput = $stderr.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) { throw "Native process failed (exit $($process.ExitCode)): $Executable`n$output`n$errorOutput" }
        return $output
    } finally { $process.Dispose() }
}

function Get-ObserverTaskName([string]$HostId) {
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    return "AIHub Skill MCP Observer - $HostId - $sid"
}

function Find-ObserverTask([string]$Name) {
    # Query only the selected host; a missing task is an explicit, repeatable-uninstall state; other native errors surface as-is.
    try { return Get-ScheduledTask -TaskName $Name -TaskPath '\' -ErrorAction Stop }
    catch {
        # The error id is 'CmdletizationQuery_NotFound_TaskName,...' on some Windows builds and
        # 'CmdletizationQuery_NotFound,Get-ScheduledTask' on others (seen on zh-CN Windows 10 22H2).
        if ($_.FullyQualifiedErrorId -like 'CmdletizationQuery_NotFound*') { return $null }
        throw
    }
}

function Stop-ObserverTask([string]$Name) {
    $task = Find-ObserverTask $Name
    if ($null -eq $task) { return }
    Stop-ScheduledTask -TaskName $Name -TaskPath '\'
    $deadline = (Get-Date).AddSeconds(20)
    do {
        $task = Find-ObserverTask $Name
        if ($null -eq $task -or $task.State -ne 'Running') { return }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)
    throw "Selected task did not stop within 20 seconds: $Name"
}

function Get-ObserverWindowlessPath([string]$Path) {
    # pythonw.exe next to python.exe runs the worker without a console window.
    if (-not $Path) { return $Path }
    $leaf = [System.IO.Path]::GetFileName($Path)
    if ($leaf -ine 'python.exe') { return $Path }
    $candidate = Join-Path ([System.IO.Path]::GetDirectoryName($Path)) 'pythonw.exe'
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    return $Path
}

function Get-ObserverInterpreterVariants([string[]]$Paths) {
    $result = @()
    foreach ($p in $Paths) { if ($p) { $result += $p; $result += (Get-ObserverWindowlessPath $p) } }
    return @($result | Select-Object -Unique)
}

function Assert-ObserverProcessArguments($Process, [string[]]$Executables, [string]$Arguments) {
    foreach ($executable in $Executables) {
        $quoted = (ConvertTo-ObserverArgument $executable) + ' ' + $Arguments
        $bare = $executable + ' ' + $Arguments
        if ($Process.CommandLine -ceq $quoted -or $Process.CommandLine -ceq $bare) { return }
        # CPython's venv launcher rewrites argv[0] to the quoted base interpreter
        # and separates it from the original arguments with two spaces.
        foreach ($head in @((ConvertTo-ObserverArgument $executable), ('"' + $executable + '"'), $executable)) {
            $pattern = '^' + [regex]::Escape($head) + ' +' + [regex]::Escape($Arguments) + '$'
            if ([regex]::IsMatch([string]$Process.CommandLine, $pattern, [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)) { return }
        }
    }
    throw ('Selected process command line does not match the exact managed worker arguments; actual: ' + $Process.CommandLine)
}

function Assert-ObserverHeartbeat([string]$TaskName, [string]$StateDirectory, [datetimeoffset]$Since, $Plan) {
    $deadline = (Get-Date).AddSeconds(40)
    $staleHeartbeat = $null
    $statusPath = Join-Path $StateDirectory 'status.json'
    do {
        $task = Find-ObserverTask $TaskName
        if ($null -eq $task) { throw "Selected task disappeared: $TaskName" }
        $launchers = Get-ObserverInterpreterVariants @($Plan.python)
        if (@($task.Actions).Count -ne 1 -or -not ($launchers -contains $task.Actions.Execute) -or $task.Actions.Arguments -cne $Plan.arguments -or $Plan.identity.launcher -ine $Plan.python) { throw 'Selected task does not match the exact managed launcher and worker arguments' }
        if (Test-Path -LiteralPath $statusPath -PathType Leaf) {
            $heartbeat = Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json
            $checked = [datetimeoffset]::Parse($heartbeat.checked_at)
            if ($checked -ge $Since) {
                if ($heartbeat.status -ne 'ok') {
                    # Upload-only failures (network, one rejected pending record) are retried from
                    # the outbox and do not depend on the installed version: they must not block.
                    $errorCodes = @($heartbeat.errors | ForEach-Object { $_.code })
                    $onlyDelivery = ($errorCodes.Count -gt 0) -and (@($errorCodes | Where-Object { $_ -ne 'delivery_failed' }).Count -eq 0)
                    if (-not $onlyDelivery) { throw "Selected worker heartbeat reports failure: $statusPath" }
                }
                if ($task.State -eq 'Running') {
                    $workerId = [uint32]$heartbeat.pid
                    if ($workerId -eq 0) { throw 'Selected heartbeat has an invalid process ID' }
                    $workerProcess = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $workerId"
                    # The heartbeat can still come from a worker that was just replaced (the task restarts
                    # it, or an older instance wrote last). Wait for the next heartbeat until the deadline.
                    if ($null -eq $workerProcess) { $staleHeartbeat = $workerId; Start-Sleep -Milliseconds 500; continue }
                    # The CPython venv shim spawns the base interpreter; the base path comes from a real probe and pyvenv.cfg.
                    $basePaths = Get-ObserverInterpreterVariants @($Plan.identity.base_executable, $Plan.identity.base_invocation)
                    if (-not ($basePaths -contains $workerProcess.ExecutablePath)) { throw 'Heartbeat process is not the verified managed base interpreter' }
                    # CPython's venv launcher starts the base interpreter with its own,
                    # unchanged command line, so argv[0] is the venv python.exe path.
                    Assert-ObserverProcessArguments $workerProcess (@($launchers) + $basePaths) $Plan.arguments
                    $launcherId = [uint32]$workerProcess.ParentProcessId
                    if ($launcherId -eq 0 -or $launcherId -eq $workerId) { throw 'Managed worker has no genuine launcher parent' }
                    $launcher = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $launcherId"
                    if ($null -eq $launcher -or -not ($launchers -contains $launcher.ExecutablePath)) { throw 'Managed base worker does not belong to the selected venv launcher' }
                    Assert-ObserverProcessArguments $launcher $launchers $Plan.arguments
                    $currentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
                    foreach ($nativeProcess in @($workerProcess,$launcher)) {
                        $owner = Invoke-CimMethod -InputObject $nativeProcess -MethodName GetOwnerSid
                        if ($owner.ReturnValue -ne 0 -or $owner.Sid -ne $currentSid) { throw 'Selected worker/launcher is not owned by the current user' }
                    }
                    return $heartbeat
                }
            }
        }
        if ($task.State -ne 'Running') {
            $info = Get-ScheduledTaskInfo -TaskName $TaskName -TaskPath '\'
            if ($info.LastRunTime -ge $Since.LocalDateTime) { throw "Selected task exited before valid heartbeat (result $($info.LastTaskResult))" }
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    if ($staleHeartbeat) { throw "Selected heartbeat process has exited (pid $staleHeartbeat) and no newer heartbeat arrived within 40 seconds: $TaskName" }
    throw "Selected worker has no fresh running heartbeat after 40 seconds: $TaskName"
}
