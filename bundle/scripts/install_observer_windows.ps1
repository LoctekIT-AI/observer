param(
    [Parameter(Mandatory = $true)][ValidateSet('dsh','codex','claude','workbuddy-desktop')][string]$HostId,
    [ValidateSet('install','status','uninstall')][string]$Mode = 'install',
    [string]$Archive,
    [Parameter(Mandatory = $true)][string]$PluginDirectory,
    [Parameter(Mandatory = $true)][string]$RuntimeDirectory,
    [Parameter(Mandatory = $true)][string]$SourceRoot,
    [Parameter(Mandatory = $true)][string]$StateDirectory,
    [string]$ConfigFile = (Join-Path $env:LOCALAPPDATA 'AIHub\observer\customer.env'),
    [switch]$Authorize,
    [string]$AuthOrigin = 'https://itsub.loctek.com/observer',
    [switch]$ReplaceConfig,
    [string]$DshProfileDirectory,
    [string]$DshCli,
    [string]$WorkBuddySettings = (Join-Path $env:USERPROFILE ".workbuddy\settings.json"),
    [string[]]$WorkBuddyPolicySettings = @()
)
. (Join-Path $PSScriptRoot 'observer_windows_common.ps1')
Assert-ObserverWindows
if (Invoke-ObserverCleanModuleRelaunch $PSCommandPath $PSBoundParameters) { exit $LASTEXITCODE }
# Protect the native profile/CLI and imported credential files before any runtime, config or archive write.
$protectedPaths = @($RuntimeDirectory,$SourceRoot,$StateDirectory,$ConfigFile) + @($DshProfileDirectory,$DshCli)
if ($HostId -eq "workbuddy-desktop") { $protectedPaths += @($WorkBuddySettings) + $WorkBuddyPolicySettings }
Assert-ObserverLayout $PluginDirectory $protectedPaths
# Save the caller's environment; restore it unchanged whether the install succeeds or a native command fails.
$observerEnvironmentNames = @('UV_PYTHON_PREFERENCE','UV_VENV_CLEAR','PYTHONPATH','PYTHONHOME','VIRTUAL_ENV','CONDA_PREFIX','NODE_OPTIONS','NPM_CONFIG_PREFIX','NPM_CONFIG_GLOBAL','UV_PYTHON_INSTALL_DIR','UV_CACHE_DIR','UV_NO_CONFIG','UV_PYTHON_BIN_DIR','NPM_CONFIG_USERCONFIG','NPM_CONFIG_GLOBALCONFIG','NPM_CONFIG_CACHE','NPM_CONFIG_STORE_DIR','PNPM_HOME','PATH')
$observerOriginalEnvironment = @{}
foreach ($name in $observerEnvironmentNames) {
    $observerOriginalEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
try {
# Only clean the environment inherited by this install process; never change user/system settings or global toolchains.
foreach ($variable in @('UV_PYTHON_PREFERENCE','UV_VENV_CLEAR','PYTHONPATH','PYTHONHOME','VIRTUAL_ENV','CONDA_PREFIX','NODE_OPTIONS','NPM_CONFIG_PREFIX','NPM_CONFIG_GLOBAL')) {
    [Environment]::SetEnvironmentVariable($variable, $null, 'Process')
}
$observerPython = Join-Path $RuntimeDirectory 'venv\Scripts\python.exe'
if ($HostId -eq 'dsh') {
    if (-not $DshProfileDirectory -or -not $DshCli -or -not (Test-Path -LiteralPath $DshProfileDirectory -PathType Container) -or -not (Test-Path -LiteralPath $DshCli -PathType Leaf)) { throw 'DSH requires explicit existing native profile and dsh/lib/bin.js (or DeepSeek Harness dsh.cmd) paths' }
}
function Get-ObserverArchive([string]$Url, [string]$Hash, [string]$Destination, [string]$Algorithm = 'SHA256') {
    Invoke-WebRequest -Uri $Url -OutFile $Destination -TimeoutSec 120 -UseBasicParsing
    $actual = (Get-FileHash -LiteralPath $Destination -Algorithm $Algorithm).Hash
    if ($Algorithm -eq 'SHA512') {
        $bytes = New-Object byte[] ($actual.Length / 2)
        for ($i=0; $i -lt $bytes.Length; $i++) { $bytes[$i] = [Convert]::ToByte($actual.Substring($i*2,2),16) }
        $actual = [Convert]::ToBase64String($bytes)
    }
    if ($actual -cne $Hash -and $actual.ToLowerInvariant() -cne $Hash) { throw "Pinned archive integrity mismatch: $Url" }
}
if ($Mode -eq 'install') {
    if (-not $Archive -or -not (Test-Path -LiteralPath $Archive -PathType Leaf)) { throw 'Install requires an existing bundle --Archive' }
    if ($HostId -ne 'workbuddy-desktop' -and -not (Test-Path -LiteralPath $SourceRoot)) { throw 'Selected native source is unavailable; no event source will be fabricated' }
    if ($ReplaceConfig -and -not $Authorize) { throw 'ReplaceConfig requires Authorize' }
    if (-not (Test-Path -LiteralPath $ConfigFile -PathType Leaf) -and -not $Authorize) { throw 'New configuration requires -Authorize through the company Observer site' }
    New-Item -ItemType Directory -Path $RuntimeDirectory -Force | Out-Null
    $downloadRoot = Join-Path $RuntimeDirectory ('downloads-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $downloadRoot | Out-Null
    try {
        $architecture = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
        switch ($architecture) {
            'X64' { $uvAsset='uv-x86_64-pc-windows-msvc.zip'; $uvHash='7c38608c8a18ee137d748a1773053b07ec8f3a30fab49aebaa6f4e4efeceb019'; $nodeArch='x64'; $nodeHash='313fa40c0d7b18575821de8cb17483031fe07d95de5994f6f435f3b345f85c66' }
            'Arm64' { $uvAsset='uv-aarch64-pc-windows-msvc.zip'; $uvHash='4b783bda5cc44bbae0651a837223873a7acee31152381aef5fc86ef6bef25997'; $nodeArch='arm64'; $nodeHash='88d36e8109736a2fa9bdc596f2cf507a3c52c69cdf96e54f8acd473ec14be853' }
            default { throw "Unsupported native Windows architecture: $architecture" }
        }
        $uvZip = Join-Path $downloadRoot 'uv.zip'
        Get-ObserverArchive "https://github.com/astral-sh/uv/releases/download/0.12.24/$uvAsset" $uvHash $uvZip
        $uvRoot = Join-Path $RuntimeDirectory 'uv-0.12.24'
        if (-not (Test-Path -LiteralPath $uvRoot)) { Expand-Archive -LiteralPath $uvZip -DestinationPath $uvRoot }
        $uvCandidates = @(Get-ChildItem -LiteralPath $uvRoot -Filter uv.exe -Recurse -File)
        if ($uvCandidates.Count -ne 1) { throw 'Pinned uv executable was not found exactly once' }
        $uv = $uvCandidates[0].FullName
        # An existing uv must match the verified download byte for byte; presence or version text is not trusted.
        $uvCheck = Join-Path $downloadRoot 'uv-check'
        Expand-Archive -LiteralPath $uvZip -DestinationPath $uvCheck
        $verifiedUv = @(Get-ChildItem -LiteralPath $uvCheck -Filter uv.exe -Recurse -File)
        if ($verifiedUv.Count -ne 1 -or (Get-FileHash -LiteralPath $uv).Hash -ne (Get-FileHash -LiteralPath $verifiedUv[0].FullName).Hash) { throw 'Existing uv differs from the pinned official archive' }
        $env:UV_PYTHON_INSTALL_DIR = Join-Path $RuntimeDirectory 'python'
        $env:UV_CACHE_DIR = Join-Path $RuntimeDirectory 'uv-cache'
        $env:UV_NO_CONFIG = '1'
        $env:UV_PYTHON_BIN_DIR = Join-Path $RuntimeDirectory 'python-bin'
        Invoke-ObserverNative $uv @('--no-config','python','install','3.12','--install-dir',$env:UV_PYTHON_INSTALL_DIR,'--no-bin') 180 | Out-Null
        if (-not (Test-Path -LiteralPath $observerPython -PathType Leaf)) {
            Invoke-ObserverNative $uv @('--no-config','venv','--managed-python','--python','3.12',(Join-Path $RuntimeDirectory 'venv')) 180 | Out-Null
        }
        $verifyPython = 'import pathlib,sys; root=pathlib.Path(sys.argv[1]).resolve(); assert sys.version_info[:2]==(3,12), "Python 3.12 required"; assert pathlib.Path(sys.base_prefix).resolve().is_relative_to(root), "Observer Python must be managed under dedicated runtime"'
        Invoke-ObserverNative $observerPython @('-c',$verifyPython,$env:UV_PYTHON_INSTALL_DIR) 30 | Out-Null
        $requirements = Join-Path (Split-Path $PSScriptRoot -Parent) 'packages\plugin-bundle\requirements-observer.txt'
        if (-not (Test-Path -LiteralPath $requirements -PathType Leaf)) { throw 'Pinned Observer requirements are unavailable' }
        Invoke-ObserverNative $uv @('--no-config','pip','install','--python',$observerPython,'--requirements',$requirements,'--only-binary',':all:') 180 | Out-Null
        # DeepSeek Harness brings its own Node and pnpm (used through its dsh.cmd launcher).
        if ($HostId -eq 'dsh' -and (Split-Path -Leaf $DshCli) -ne 'dsh.cmd') {
            $nodeAsset = "node-v24.14.0-win-$nodeArch.zip"
            $nodeZip = Join-Path $downloadRoot 'node.zip'
            Get-ObserverArchive "https://nodejs.org/dist/v24.14.0/$nodeAsset" $nodeHash $nodeZip
            $nodeCheck = Join-Path $downloadRoot 'node-check'
            Expand-Archive -LiteralPath $nodeZip -DestinationPath $nodeCheck
            $verifiedNode = Join-Path $nodeCheck "node-v24.14.0-win-$nodeArch"
            $nodeRoot = Join-Path $RuntimeDirectory 'node'
            if (-not (Test-Path -LiteralPath $nodeRoot)) { Move-Item -LiteralPath $verifiedNode -Destination $nodeRoot } else {
                foreach ($verifiedFile in (Get-ChildItem -LiteralPath $verifiedNode -Recurse -File)) {
                    $relative = $verifiedFile.FullName.Substring($verifiedNode.Length + 1)
                    $installedFile = Join-Path $nodeRoot $relative
                    if (-not (Test-Path -LiteralPath $installedFile -PathType Leaf) -or (Get-FileHash -LiteralPath $installedFile).Hash -ne (Get-FileHash -LiteralPath $verifiedFile.FullName).Hash) { throw "Existing Node file differs from pinned official archive: $relative" }
                }
            }
            $node = Join-Path $nodeRoot 'node.exe'
            $pnpmArchive = Join-Path $downloadRoot 'pnpm.tgz'
            Get-ObserverArchive 'https://registry.npmjs.org/pnpm/-/pnpm-11.25.0.tgz' 'XN6SW08HX3Jetx+64YpC/+eEUkeJ8ZthxzHLhyHsKKruFg4BqNWvT+2ypCzb8wDv4j2zVrDUoXtNY+EfirfJVg==' $pnpmArchive 'SHA512'
            $pnpmRoot = Join-Path $RuntimeDirectory 'pnpm'
            New-Item -ItemType Directory -Path $pnpmRoot -Force | Out-Null
            $env:NPM_CONFIG_USERCONFIG = Join-Path $RuntimeDirectory 'npm-user.conf'
            $env:NPM_CONFIG_GLOBALCONFIG = Join-Path $RuntimeDirectory 'npm-global.conf'
            $env:NPM_CONFIG_CACHE = Join-Path $RuntimeDirectory 'npm-cache'
            $env:NPM_CONFIG_STORE_DIR = Join-Path $RuntimeDirectory 'pnpm-store'
            $env:PNPM_HOME = Join-Path $RuntimeDirectory 'pnpm-home'
            foreach ($config in @($env:NPM_CONFIG_USERCONFIG,$env:NPM_CONFIG_GLOBALCONFIG)) {
                if (-not (Test-Path -LiteralPath $config)) { New-Item -ItemType File -Path $config | Out-Null }
            }
            $env:PATH = "$nodeRoot;$pnpmRoot;$env:PATH"
            Invoke-ObserverNative $node @((Join-Path $nodeRoot 'node_modules\npm\bin\npm-cli.js'),'install','--global=false','--prefix',$pnpmRoot,'--ignore-scripts','--no-audit','--no-fund','--package-lock=false',$pnpmArchive) 180 | Out-Null
            $env:PATH = "$(Join-Path $pnpmRoot 'node_modules\.bin');$env:PATH"
            Invoke-ObserverNative $node @((Join-Path $pnpmRoot 'node_modules\pnpm\bin\pnpm.cjs'),'--version') 30 | Out-Null
        }
        if ($Authorize -and -not $ReplaceConfig -and (Test-Path -LiteralPath $ConfigFile -PathType Leaf) -and (Select-String -LiteralPath $ConfigFile -Pattern '^OBSERVER_AUTH_TOKEN_REF=.' -Quiet) -and (Select-String -LiteralPath $ConfigFile -Pattern ('^OBSERVER_AUTH_ORIGIN=' + [regex]::Escape($AuthOrigin.TrimEnd('/')) + '$') -Quiet)) {
            Write-Host 'Observer: existing Observer login found; reusing it without a new login'
        } elseif ($Authorize) {
            $authArgs = @((Join-Path $PSScriptRoot 'observer_authorize.py'),'--client-type',$HostId,'--config-file',$ConfigFile,'--auth-origin',$AuthOrigin)
            if ($ReplaceConfig) { $authArgs += '--replace' }
            Invoke-ObserverNative $observerPython $authArgs 660 | Out-Null
        }
    } finally {
        # .NET delete instead of Remove-Item: some AI clients (e.g. WorkBuddy) replace Remove-Item in
        # their PowerShell sessions with an approval wrapper that fails when nobody can approve.
        # This is only Observer's own temporary download folder, so a failed cleanup is not fatal.
        try { [System.IO.Directory]::Delete($downloadRoot, $true) } catch { Write-Warning "Observer: could not remove temporary folder $downloadRoot" }
    }
}
$arguments = @((Join-Path $PSScriptRoot 'observer_windows.py'),$Mode,'--host',$HostId,'--install-root',$PluginDirectory,'--python',$observerPython,'--source-root',$SourceRoot,'--state-dir',$StateDirectory,'--config-file',$ConfigFile,'--runtime-dir',$RuntimeDirectory)
if ($Mode -eq 'install') { $arguments += @('--archive',$Archive) }
if ($HostId -eq 'dsh') { $arguments += @('--dsh-profile',$DshProfileDirectory,'--dsh-cli',$DshCli) }
if ($HostId -eq "workbuddy-desktop") {
    $arguments += @("--workbuddy-settings",$WorkBuddySettings)
    foreach ($policy in $WorkBuddyPolicySettings) { $arguments += @("--workbuddy-policy-settings",$policy) }
}
# On Windows, running Observer workers keep the shared bundle folder busy (open
# files and working directory), so a new bundle cannot be activated while any of
# them runs. Pause every Observer task of this user first and restart the other
# hosts afterwards; the selected host is restarted by the install itself.
$pausedTasks = @()
$selectedWasRunning = $false
if ($Mode -eq 'install') {
    foreach ($otherHost in @('dsh','codex','claude','workbuddy-desktop')) {
        $otherName = Get-ObserverTaskName $otherHost
        $otherTask = Find-ObserverTask $otherName
        if ($null -ne $otherTask -and $otherTask.State -eq 'Running') {
            Stop-ObserverTask $otherName
            if ($otherHost -eq $HostId) { $selectedWasRunning = $true } else { $pausedTasks += $otherName }
        }
    }
    $workerScripts = (Join-Path $PluginDirectory 'scripts').TrimEnd('\') + '\'
    $deadline = (Get-Date).AddSeconds(20)
    do {
        $leftovers = @(Get-CimInstance -ClassName Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.CommandLine.IndexOf($workerScripts, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and $_.CommandLine -match '_observer_worker\.py' })
        if ($leftovers.Count -eq 0) { break }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    foreach ($leftover in $leftovers) { Stop-Process -Id $leftover.ProcessId -Force -ErrorAction SilentlyContinue }
}
$installSucceeded = $false
try {
    Invoke-ObserverNative $observerPython $arguments 1260
    $installSucceeded = $true
} finally {
    foreach ($pausedName in $pausedTasks) {
        try { Start-ScheduledTask -TaskName $pausedName -TaskPath '\' } catch { Write-Warning "Observer task could not be restarted: $pausedName" }
    }
    if (-not $installSucceeded -and $selectedWasRunning) {
        $selectedName = Get-ObserverTaskName $HostId
        if ($null -ne (Find-ObserverTask $selectedName)) {
            try { Start-ScheduledTask -TaskName $selectedName -TaskPath '\' } catch { Write-Warning "Observer task could not be restarted: $selectedName" }
        }
    }
}
} finally {
    foreach ($name in $observerEnvironmentNames) {
        [Environment]::SetEnvironmentVariable($name, $observerOriginalEnvironment[$name], 'Process')
    }
}
