# Observer 安装（Windows）。不需要 AI 助手：双击 Install-Observer-Windows.cmd，或在 PowerShell 里运行
#   irm https://raw.githubusercontent.com/LoctekIT-AI/observer/main/install/install-windows.ps1 | iex
# 步骤：读取签名的版本文件 → 下载安装包并校验 SHA-256 → 检测本机客户端 → 征得同意后安装（飞书登录一次）。
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Repo = 'LoctekIT-AI/observer'
$Channel = if ($env:OBSERVER_CHANNEL) { $env:OBSERVER_CHANNEL } else { 'test' }
$AssumeYes = $env:OBSERVER_ASSUME_YES -eq '1'

function Stop-Install([string]$Message) {
    Write-Host ''
    Write-Host "安装没有完成：$Message" -ForegroundColor Red
    throw $Message
}

Write-Host ''
Write-Host '==== Observer 安装 ====' -ForegroundColor Cyan
Write-Host 'Observer 是公司内部部署的 AI 使用统计工具：统计你在 Codex、Claude、AI Hub（DSH）、WorkBuddy 里调用了哪些 Skill 和 MCP（调用了哪个、什么时候、成功还是失败），按飞书登录的工号和姓名归属。'
Write-Host '不会采集对话内容、提示词、模型输出、工具参数和返回内容。'
Write-Host '会在本机安装：后台收集程序（当前用户的计划任务）、自动更新任务（只安装公司签名的新版本）；WorkBuddy 会加 3 个钩子，AI Hub 会加一个 Observer 插件。'
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
Write-Host ''
Write-Host '开始安装。第一次安装会打开浏览器，请用飞书登录；之后不会再要求登录。'
& $powershell -NoProfile -ExecutionPolicy Bypass -File $all -Archive $archive -Hosts ($selected -join ',')
if ($LASTEXITCODE -ne 0) { Stop-Install "安装程序返回 $LASTEXITCODE，请把上面的输出发给 AI 平台团队" }
Write-Host ''
Write-Host '安装完成。之后采集和上报都在后台自动进行，也会自动更新。' -ForegroundColor Green
if ($selected -contains 'dsh' -or $selected -contains 'workbuddy-desktop') {
    Write-Host '请把 AI Hub 和 WorkBuddy 完全退出（包括托盘图标）再打开一次。' -ForegroundColor Yellow
}
