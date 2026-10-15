# One-sentence install (Windows): detect the desktop AI clients installed on this
# machine and run install_observer_windows.ps1 for each of them.
# Only the first install needs the Feishu login; later clients reuse it.
# -DetectOnly: print the detection result and change nothing.
param(
    [string]$Archive,
    [string]$Root = (Join-Path $env:LOCALAPPDATA 'Observer'),
    [string]$ConfigFile = (Join-Path $env:LOCALAPPDATA 'AIHub\observer\customer.env'),
    [string]$AuthOrigin = 'https://itsub.loctek.com/observer',
    [string[]]$Hosts = @(),
    [ValidateSet('install','status')][string]$Mode = 'install',
    [switch]$DetectOnly,
    # Used by the self-updater: never open a login page, skip clients that are no longer installed.
    [switch]$Unattended
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'observer_windows_common.ps1')
if (Invoke-ObserverCleanModuleRelaunch $PSCommandPath $PSBoundParameters) { exit $LASTEXITCODE }
$codexSource = Join-Path $env:USERPROFILE '.codex\sessions'
$claudeSource = Join-Path $env:APPDATA 'Claude\IndexedDB\https_claude.ai_0.indexeddb.leveldb'
if (-not (Test-Path -LiteralPath $claudeSource -PathType Container)) {
    # Claude installed from the Microsoft Store keeps its data inside the package folder.
    $storeClaude = Get-ChildItem -LiteralPath (Join-Path $env:LOCALAPPDATA 'Packages') -Directory -Filter 'Claude_*' -ErrorAction SilentlyContinue |
        ForEach-Object { Join-Path $_.FullName 'LocalCache\Roaming\Claude\IndexedDB\https_claude.ai_0.indexeddb.leveldb' } |
        Where-Object { Test-Path -LiteralPath $_ -PathType Container } | Select-Object -First 1
    if ($storeClaude) { $claudeSource = $storeClaude }
}
$workbuddySettings = Join-Path $env:USERPROFILE '.workbuddy\settings.json'
$dshProfile = Join-Path $env:APPDATA 'dsh-desktop\harness\profiles\web'
$dshCliCandidates = @(
    (Join-Path $env:LOCALAPPDATA 'Programs\DSH Desktop\resources\app\node_modules\@deepseek-ai\dsh\lib\bin.js'),
    (Join-Path ${env:ProgramFiles} 'DSH Desktop\resources\app\node_modules\@deepseek-ai\dsh\lib\bin.js')
)
$dshCli = $dshCliCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
# DeepSeek Harness builds run DSH from app.asar through the app's own launcher
# resources\runtime\cli\bin\dsh.cmd. AI Hub (the company build) keeps its DSH home
# under %APPDATA%\AI Hub\ah-home; older Harness builds use ~\.dsh. Both use the
# reserved "desktop" profile. AI Hub is preferred when both are set up.
$harnessCandidates = @(
    @{ Profile = (Join-Path $env:APPDATA 'AI Hub\ah-home\profiles\desktop'); Name = 'AI Hub'; Dirs = @((Join-Path $env:LOCALAPPDATA 'Programs\AI Hub'), (Join-Path ${env:ProgramFiles} 'AI Hub')) },
    @{ Profile = (Join-Path $env:USERPROFILE '.dsh\profiles\desktop'); Name = 'DeepSeek Harness'; Dirs = @((Join-Path $env:LOCALAPPDATA 'Programs\DeepSeek Harness'), (Join-Path ${env:ProgramFiles} 'DeepSeek Harness')) }
)
foreach ($candidate in $harnessCandidates) {
    if ($dshCli) { break }
    if (-not (Test-Path -LiteralPath (Join-Path $candidate.Profile 'package.json') -PathType Leaf)) { continue }
    $harnessDirs = @()
    $harnessDirs += @(Get-Process -Name $candidate.Name -ErrorAction SilentlyContinue | ForEach-Object { try { Split-Path -Parent $_.Path } catch { } })
    foreach ($key in @('HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*', 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*', 'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*')) {
        # Strict mode: uninstall entries do not all have DisplayName / InstallLocation / DisplayIcon.
        $harnessDirs += @(Get-ItemProperty -Path $key -ErrorAction SilentlyContinue | ForEach-Object {
            $props = $_.PSObject.Properties
            if ($props['DisplayName'] -and "$($props['DisplayName'].Value)" -like ($candidate.Name + '*')) {
                if ($props['InstallLocation'] -and $props['InstallLocation'].Value) { "$($props['InstallLocation'].Value)".Trim('"') }
                elseif ($props['DisplayIcon'] -and $props['DisplayIcon'].Value) { Split-Path -Parent ("$($props['DisplayIcon'].Value)".Trim('"') -replace ',\d+$', '') }
            } })
    }
    $harnessDirs += $candidate.Dirs
    $dshCli = $harnessDirs | Where-Object { $_ } | ForEach-Object { Join-Path $_ 'resources\runtime\cli\bin\dsh.cmd' } |
        Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    if ($dshCli) { $dshProfile = $candidate.Profile }
}
$detected = [ordered]@{
    'codex' = (Test-Path -LiteralPath $codexSource -PathType Container)
    'claude' = (Test-Path -LiteralPath $claudeSource -PathType Container)
    'dsh' = ((Test-Path -LiteralPath (Join-Path $dshProfile 'package.json') -PathType Leaf) -and [bool]$dshCli)
    'workbuddy-desktop' = (Test-Path -LiteralPath $workbuddySettings -PathType Leaf)
}
if (-not $detected['dsh'] -and ((Test-Path -LiteralPath (Join-Path $env:USERPROFILE '.dsh\profiles') -PathType Container) -or (Test-Path -LiteralPath (Join-Path $env:APPDATA 'AI Hub\ah-home\profiles') -PathType Container))) {
    Write-Output "dsh`tfound-incomplete (an AI Hub / DeepSeek Harness profile is present but its launcher was not found; skipped)"
}
foreach ($name in $detected.Keys) { Write-Output ("{0}`t{1}" -f $name, $(if ($detected[$name]) { 'found' } else { 'missing' })) }
# 'powershell -File' passes '-Hosts a,b' as one string: accept comma or space separated names.
$Hosts = @($Hosts | ForEach-Object { $_ -split '[,\s]+' } | Where-Object { $_ })
# @(...) keeps a single selected client an array (strict mode has no .Count on a scalar).
$selected = @(if ($Hosts.Count -gt 0) { $Hosts } else { $detected.Keys | Where-Object { $detected[$_] } })
Write-Output ("HOSTS={0}" -f ($selected -join ' '))
if ($DetectOnly) { return }
if ($selected.Count -eq 0) { throw 'No supported desktop AI client was detected' }
if ($Mode -eq 'install' -and -not $Archive) { throw '-Archive is required for install' }
$single = Join-Path $PSScriptRoot 'install_observer_windows.ps1'
foreach ($hostId in $selected) {
    if (-not $detected[$hostId]) {
        if ($Unattended) { Write-Output "=== skip $hostId (not detected on this machine)"; continue }
        throw "Selected client $hostId was not detected on this machine"
    }
    $common = @{
        HostId = $hostId; Mode = $Mode; ConfigFile = $ConfigFile; AuthOrigin = $AuthOrigin
        PluginDirectory = (Join-Path $Root 'bundle'); RuntimeDirectory = (Join-Path $Root 'runtime')
        StateDirectory = (Join-Path $Root "state\$hostId")
    }
    if ($Mode -eq 'install') { $common.Archive = $Archive; if (-not $Unattended) { $common.Authorize = $true } }
    switch ($hostId) {
        'codex' { $common.SourceRoot = $codexSource }
        'claude' { $common.SourceRoot = $claudeSource }
        'workbuddy-desktop' { $common.SourceRoot = (Join-Path $Root 'workbuddy\workbuddy-hook-events.jsonl'); $common.WorkBuddySettings = $workbuddySettings }
        'dsh' {
            $common.SourceRoot = (Join-Path $env:USERPROFILE '.aihub\observer\dsh'); $common.DshProfileDirectory = $dshProfile; $common.DshCli = $dshCli
            # Observer's own event folder; the DSH plugin appends to it once DSH loads the plugin.
            if ($Mode -eq 'install') { New-Item -ItemType Directory -Path $common.SourceRoot -Force | Out-Null }
        }
    }
    Write-Output "=== $Mode $hostId"
    & $single @common
}
if ($Mode -eq 'install') {
    # Signed self-update (scripts\observer_update.py). The collectors keep working without it.
    try {
        & (Join-Path $PSScriptRoot 'register_observer_updater_windows.ps1') -Mode install -PythonExecutable (Join-Path $Root 'runtime\venv\Scripts\python.exe') -UpdaterScript (Join-Path $Root 'bundle\scripts\observer_update.py') -StateDirectory (Join-Path $Root 'updater')
    } catch {
        Write-Warning "Observer: the self-update task could not be registered: $($_.Exception.Message)"
    }
}
