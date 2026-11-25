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

# A symbolic link or junction (not other reparse points such as OneDrive or deduplicated files).
function Test-ObserverLink($Item) {
    if (($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -eq 0) { return $false }
    $linkType = $null
    if ($Item.PSObject.Properties['LinkType']) { $linkType = $Item.LinkType }
    return ($linkType -in @('SymbolicLink', 'Junction')) -or ($Item.PSObject.Properties['Target'] -and @($Item.Target | Where-Object { $_ }).Count -gt 0)
}

# The real location of a path: every symbolic link or junction on the way is replaced by its
# target (e.g. C:\Users\me\.codex -> D:\data\.codex). Parts that do not exist yet are kept.
# Installers then work only with real locations, so nothing is written or removed through a link.
function Get-ObserverCanonicalPath([string]$Value) {
    if (-not $Value) { return $Value }
    $full = [System.IO.Path]::GetFullPath($Value)
    for ($hop = 0; $hop -lt 16; $hop++) {
        $root = [System.IO.Path]::GetPathRoot($full)
        $separator = [System.IO.Path]::DirectorySeparatorChar
        $parts = @($full.Substring($root.Length).Split($separator) | Where-Object { $_ })
        $prefix = $root
        $replaced = $false
        for ($index = 0; $index -lt $parts.Count; $index++) {
            $next = Join-Path $prefix $parts[$index]
            if (-not (Test-Path -LiteralPath $next)) { break }
            $item = Get-Item -LiteralPath $next -Force
            if (Test-ObserverLink $item) {
                $target = @($item.Target | Where-Object { $_ }) | Select-Object -First 1
                if (-not $target) { throw "Observer cannot resolve the link $next" }
                $target = [string]$target
                if ($target.StartsWith('\\?\')) { $target = $target.Substring(4) }
                if (-not [System.IO.Path]::IsPathRooted($target)) { $target = Join-Path $prefix $target }
                $rest = @(if ($index + 1 -lt $parts.Count) { $parts[($index + 1)..($parts.Count - 1)] })
                $full = if ($rest.Count -gt 0) { [System.IO.Path]::GetFullPath((Join-Path $target ($rest -join $separator))) } else { [System.IO.Path]::GetFullPath($target) }
                $replaced = $true
                break
            }
            $prefix = $next
        }
        if (-not $replaced) { return $full }
    }
    throw "Observer found too many links in $Value"
}

function Resolve-ObserverInputPath([string]$Value) {
    if ($Value -notmatch '^(?:[A-Za-z]:\\|\\\\[^\\]+\\[^\\]+(?:\\|$))' -or $Value.StartsWith('\\?\') -or $Value.StartsWith('\\.\') -or $Value.Contains('"') -or $Value.Contains([char]0) -or $Value.Contains("`n") -or $Value.Contains("`r")) { throw 'Observer inputs require explicit supported absolute paths' }
    $full = [System.IO.Path]::GetFullPath($Value)
    $candidate = $full
    while ($candidate) {
        if (Test-Path -LiteralPath $candidate) {
            $item = Get-Item -LiteralPath $candidate -Force
            # PS5.1 has no unified link canonicalization API; reject reparse aliases and require explicit real paths to avoid out-of-bounds deletes.
            if (Test-ObserverLink $item) { throw 'Observer input contains a link or junction; use its explicit canonical path' }
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


# ---- Startup-item fallback --------------------------------------------------------------
# Some company computers refuse to create scheduled tasks for a normal user (security
# software or policy: Register-ScheduledTask and schtasks both answer "access denied").
# There the installer writes a job file per program into <Observer root>\startup and
# registers observer_supervisor_windows.py as a per-user startup item (HKCU ... Run);
# the supervisor starts and restarts the programs. Machines that allow tasks are unchanged.

$ObserverStartupValueName = 'AIHub Observer'
$ObserverRunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'

function Test-ObserverAccessDenied($ErrorRecord) {
    $exception = $ErrorRecord.Exception
    while ($null -ne $exception) {
        if ($exception.HResult -eq -2147024891) { return $true }  # 0x80070005 E_ACCESSDENIED
        if ($exception -is [System.UnauthorizedAccessException]) { return $true }
        $exception = $exception.InnerException
    }
    if ([string]$ErrorRecord.CategoryInfo.Category -eq 'PermissionDenied') { return $true }
    return ([string]$ErrorRecord.FullyQualifiedErrorId) -like '*0x80070005*'
}

function Write-ObserverUtf8File([string]$Path, [string]$Text) {
    $directory = [System.IO.Path]::GetDirectoryName($Path)
    if (-not (Test-Path -LiteralPath $directory)) { New-Item -ItemType Directory -Force -Path $directory | Out-Null }
    $temporary = $Path + '.tmp'
    [System.IO.File]::WriteAllText($temporary, $Text, (New-Object System.Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Get-ObserverStartupJob([string]$JobFile) {
    if (-not (Test-Path -LiteralPath $JobFile -PathType Leaf)) { return $null }
    return [System.IO.File]::ReadAllText($JobFile)
}

function Set-ObserverStartupJob([string]$JobFile, [hashtable]$Job) {
    $Job['generation'] = [datetimeoffset]::Now.ToString('o')
    Write-ObserverUtf8File $JobFile ($Job | ConvertTo-Json -Compress)
}

function Enable-ObserverSupervisor([string]$JobsDirectory, [string]$PythonExecutable, [string]$SupervisorScript) {
    if (-not (Test-Path -LiteralPath $SupervisorScript -PathType Leaf)) { throw "Observer supervisor script is missing: $SupervisorScript" }
    $pythonw = Get-ObserverWindowlessPath $PythonExecutable
    $arguments = (ConvertTo-ObserverArgument $SupervisorScript) + ' --jobs-dir ' + (ConvertTo-ObserverArgument $JobsDirectory)
    $command = (ConvertTo-ObserverArgument $pythonw) + ' ' + $arguments
    if (-not (Test-Path -LiteralPath $ObserverRunKey)) { New-Item -Path $ObserverRunKey -Force | Out-Null }
    Set-ItemProperty -LiteralPath $ObserverRunKey -Name $ObserverStartupValueName -Value $command -Type String
    # Start it now as well; a second copy exits at once when one is already running.
    Start-Process -FilePath $pythonw -ArgumentList $arguments -WorkingDirectory $JobsDirectory -WindowStyle Hidden | Out-Null
}

function Disable-ObserverSupervisorIfIdle([string]$JobsDirectory) {
    $jobs = @(if (Test-Path -LiteralPath $JobsDirectory) { Get-ChildItem -LiteralPath $JobsDirectory -Filter '*.json' -File })
    if ($jobs.Count -gt 0) { return $false }
    $properties = Get-ItemProperty -LiteralPath $ObserverRunKey -ErrorAction SilentlyContinue
    if ($null -ne $properties -and $null -ne $properties.PSObject.Properties[$ObserverStartupValueName]) {
        Remove-ItemProperty -LiteralPath $ObserverRunKey -Name $ObserverStartupValueName
    }
    return $true  # the supervisor exits by itself once it sees no jobs
}

function Stop-ObserverStartupWorker([string]$StateDirectory) {
    # The supervisor stops a worker whose job file is gone; this also covers a worker left
    # by a supervisor that is no longer running. Only a process whose command line names
    # this state folder is stopped.
    $statusPath = Join-Path $StateDirectory 'status.json'
    if (-not (Test-Path -LiteralPath $statusPath -PathType Leaf)) { return }
    try { $workerId = [uint32]((Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json).pid) } catch { return }
    if ($workerId -eq 0) { return }
    $process = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $workerId" -ErrorAction SilentlyContinue
    if ($null -eq $process -or -not ([string]$process.CommandLine).Contains($StateDirectory.TrimEnd('\'))) { return }
    $parent = Get-CimInstance -ClassName Win32_Process -Filter ("ProcessId = " + [uint32]$process.ParentProcessId) -ErrorAction SilentlyContinue
    Stop-Process -Id $workerId -Force -ErrorAction SilentlyContinue
    # The venv launcher (pythonw.exe of the venv) waits for its child; stop it too when it runs this worker.
    if ($null -ne $parent -and ([string]$parent.CommandLine).Contains($StateDirectory.TrimEnd('\'))) { Stop-Process -Id $parent.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Assert-ObserverStartupHeartbeat([string]$StateDirectory, [datetimeoffset]$Since, $Plan, [int]$Seconds = 75) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    $statusPath = Join-Path $StateDirectory 'status.json'
    do {
        if (Test-Path -LiteralPath $statusPath -PathType Leaf) {
            try { $heartbeat = Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json } catch { $heartbeat = $null }
            if ($null -ne $heartbeat -and [datetimeoffset]::Parse($heartbeat.checked_at) -ge $Since) {
                if ($heartbeat.status -ne 'ok') {
                    $errorCodes = @($heartbeat.errors | ForEach-Object { $_.code })
                    $onlyDelivery = ($errorCodes.Count -gt 0) -and (@($errorCodes | Where-Object { $_ -ne 'delivery_failed' }).Count -eq 0)
                    if (-not $onlyDelivery) { throw "Selected worker heartbeat reports failure: $statusPath" }
                }
                $workerId = [uint32]$heartbeat.pid
                $process = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $workerId" -ErrorAction SilentlyContinue
                if ($null -ne $process -and ([string]$process.CommandLine).Contains([string]$Plan.arguments)) { return $heartbeat }
            }
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    throw "Selected worker has no fresh running heartbeat after $Seconds seconds (startup item): $StateDirectory"
}
