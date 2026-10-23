# Observer 安装（Windows）。不需要 AI 助手：双击 Install-Observer-Windows.cmd，或在 PowerShell 里运行
#   irm https://raw.githubusercontent.com/LoctekIT-AI/observer/main/install/install-windows.ps1 | iex
# 步骤：读取签名的版本文件 → 下载安装包并校验 SHA-256 → 检测本机客户端 → 征得同意后安装（飞书登录一次）。
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Repo = 'LoctekIT-AI/observer'
# Production (release) by default; set OBSERVER_CHANNEL=test for the test environment.
$Channel = if ($env:OBSERVER_CHANNEL) { $env:OBSERVER_CHANNEL } else { 'release' }
$AssumeYes = $env:OBSERVER_ASSUME_YES -eq '1'

function Stop-Install([string]$Message) {
    Write-Host ''
    Write-Host "安装没有完成：$Message" -ForegroundColor Red
    throw $Message
}

Write-Host ''
Write-Host '==== Observer 安装 ====' -ForegroundColor Cyan
Write-Host ''

# 1. 签名的版本文件（自动更新用的同一份）
$channelUrl = "https://raw.githubusercontent.com/$Repo/main/channels/$Channel.json"
Write-Host "读取最新版本：$channelUrl"
$entry = Invoke-RestMethod -UseBasicParsing -Uri $channelUrl -TimeoutSec 60
$suffix = if ($Channel -eq 'test') { '-test' } else { '' }
if ("$($entry.version)" -notmatch '^\d+\.\d+\.\d+$' -or $entry.tag -ne "v$($entry.version)$suffix" -or $entry.asset -ne "observer-v$($entry.version)$suffix.zip" -or "$($entry.sha256)" -notmatch '^[0-9a-f]{64}$') {
    Stop-Install '版本文件格式不对'
}
Write-Host "最新版本：$($entry.tag)"

# 2. 下载并校验
$archives = Join-Path $env:LOCALAPPDATA 'AIHub\observer\archives'
New-Item -ItemType Directory -Force -Path $archives | Out-Null
$archive = Join-Path $archives $entry.asset
$url = "https://github.com/$Repo/releases/download/$($entry.tag)/$($entry.asset)"
Write-Host "下载安装包：$url"
Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $archive -TimeoutSec 600
$actual = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actual -ne $entry.sha256 -or (Get-Item -LiteralPath $archive).Length -ne [int64]$entry.size) {
    Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
    Stop-Install '安装包校验失败（SHA-256 不一致）'
}
Write-Host '安装包校验通过。'
$work = Join-Path $env:TEMP ('observer-install-' + $entry.version)
if (Test-Path -LiteralPath $work) { [System.IO.Directory]::Delete($work, $true) }
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::ExtractToDirectory($archive, $work)
$all = Join-Path $work 'scripts\install_observer_windows_all.ps1'
$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

# 3. 检测客户端
Write-Host ''
Write-Host '检测本机的 AI 客户端：'
$detect = & $powershell -NoProfile -ExecutionPolicy Bypass -File $all -DetectOnly 2>&1 | ForEach-Object { "$_" }
$detect | Where-Object { $_ -notmatch '^HOSTS=' } | ForEach-Object { Write-Host "  $_" }
$hostsLine = $detect | Where-Object { $_ -match '^HOSTS=' } | Select-Object -Last 1
$found = @(if ($hostsLine) { ($hostsLine -replace '^HOSTS=', '') -split '\s+' | Where-Object { $_ } })
if ($found.Count -eq 0) { Stop-Install '没有检测到支持的客户端（Codex、Claude、AI Hub、WorkBuddy）。AI Hub 需要先打开过一次。' }

# 4. 同意
$selected = $found
if (-not $AssumeYes) {
    Write-Host ''
    Write-Host ("将为这些客户端安装：{0}" -f ($found -join '、'))
    $answer = Read-Host '全部安装请输入 Y；只装其中几个请输入名称（用逗号分隔）；直接回车取消'
    if (-not $answer) { Write-Host '已取消，没有做任何改动。'; return }
    if ($answer -notmatch '^[Yy]$') {
        $selected = @($answer -split '[,，\s]+' | Where-Object { $_ })
        foreach ($name in $selected) { if ($found -notcontains $name) { Stop-Install "没有检测到客户端：$name" } }
    }
}

# 5. 安装（第一次会打开浏览器，用飞书登录一次）
# 记下安装前本机有哪些 WorkBuddy（安装会写钩子，之后就分不清了），结尾只提示真正装了的客户端。
$hadWorkBuddy = Test-Path -LiteralPath (Join-Path $env:USERPROFILE '.workbuddy\settings.json') -PathType Leaf
$hadWorkBuddyAi = Test-Path -LiteralPath (Join-Path $env:USERPROFILE '.workbuddy-ai\settings.json') -PathType Leaf
Write-Host ''
Write-Host '开始安装。第一次安装会打开浏览器，请用飞书登录；之后不会再要求登录。'
$resultFile = Join-Path $work 'install-result.tsv'
$allArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $all, '-Archive', $archive, '-Hosts', ($selected -join ','))
# Packages from v0.2.35 report each client separately; older ones stop at the first failure.
$supportsResult = $false
try { $supportsResult = (Get-Command -Name $all -ErrorAction Stop).Parameters.ContainsKey('ResultFile') } catch { }
if ($supportsResult) { $allArgs += @('-ResultFile', $resultFile) }
& $powershell @allArgs
$code = $LASTEXITCODE
$failed = @()
if (Test-Path -LiteralPath $resultFile) {
    $failed = @(Get-Content -LiteralPath $resultFile -Encoding UTF8 | Where-Object { $_ -match '^failed\t' } | ForEach-Object { ($_ -split "`t")[1] })
}
if ($code -ne 0 -and -not ($code -eq 3 -and $failed.Count -gt 0)) { Stop-Install "安装程序返回 $code，请把上面的输出发给 AI 平台团队" }
$installed = @($selected | Where-Object { $failed -notcontains $_ })
Write-Host ''
Write-Host ("安装完成：{0}。之后采集和上报都在后台自动进行，也会自动更新。" -f ($installed -join '、')) -ForegroundColor Green
if ($failed.Count -gt 0) {
    Write-Host ("以下客户端没有装上：{0}。其他客户端不受影响；请把上面的输出发给 AI 平台团队。" -f ($failed -join '、')) -ForegroundColor Yellow
}
$restart = @()
if ($installed -contains 'dsh') { $restart += 'AI Hub' }
if ($installed -contains 'workbuddy-desktop') {
    if ($hadWorkBuddy) { $restart += 'WorkBuddy' }
    if ($hadWorkBuddyAi) { $restart += 'WorkBuddy AI' }
}
if ($restart.Count -gt 0) {
    Write-Host ('请把 {0} 完全退出（包括托盘图标）再打开一次。' -f ($restart -join '、')) -ForegroundColor Yellow
}
