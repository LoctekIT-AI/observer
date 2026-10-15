#!/bin/bash
# 只读：检测本机已安装、且有原生数据目录的桌面 AI 客户端。不修改任何文件。
# 每台电脑装了哪些客户端都不一定：只要求应用和数据都在（Claude、WorkBuddy 要能读到应用版本）。
# 输出每个客户端一行：<host> <found|missing> <原因>；最后一行 HOSTS=<可安装的 host 列表>。
set -euo pipefail
hosts=()
report() { printf '%s\t%s\t%s\n' "$1" "$2" "$3"; }
codex_source="$HOME/.codex/sessions"
if [ -d "$codex_source" ]; then report codex found "$codex_source"; hosts+=(codex); else report codex missing "no $codex_source"; fi
claude_source="$HOME/Library/Application Support/Claude/IndexedDB/https_claude.ai_0.indexeddb.leveldb"
claude_app=''
for candidate in '/Applications/Claude.app' "$HOME/Applications/Claude.app"; do
    if [ -f "$candidate/Contents/Info.plist" ]; then claude_app=$candidate; break; fi
done
if [ -z "$claude_app" ] && [ -x /usr/bin/mdfind ]; then
    claude_app=$(/usr/bin/mdfind "kMDItemCFBundleIdentifier == 'com.anthropic.claudefordesktop'" 2>/dev/null | /usr/bin/grep '\.app$' | /usr/bin/head -n 1 || true)
    [ -n "$claude_app" ] && [ -f "$claude_app/Contents/Info.plist" ] || claude_app=''
fi
if [ ! -d "$claude_source" ]; then report claude missing "no Claude Desktop IndexedDB"
elif [ -z "$claude_app" ]; then report claude missing "Claude Desktop data found, but the Claude app is not installed"
else report claude found "$claude_source"; hosts+=(claude); fi
# AI Hub (the company build of DeepSeek Harness) first, then the older DSH Desktop layout.
aihub_profile="$HOME/Library/Application Support/AI Hub/ah-home/profiles/desktop"
aihub_cli='/Applications/AI Hub.app/Contents/Resources/runtime/cli/bin/dsh'
dsh_profile="$HOME/Library/Application Support/dsh-desktop/harness/profiles/web"
dsh_cli='/Applications/DSH Desktop.app/Contents/Resources/app/node_modules/@deepseek-ai/dsh/lib/bin.js'
if [ -f "$aihub_profile/package.json" ] && [ -f "$aihub_cli" ]; then report dsh found "$aihub_profile (AI Hub)"; hosts+=(dsh)
elif [ -f "$dsh_profile/package.json" ] && [ -f "$dsh_cli" ]; then report dsh found "$dsh_profile"; hosts+=(dsh)
elif [ -f "$aihub_cli" ]; then report dsh missing "AI Hub is installed but has not been opened yet (no desktop profile); open AI Hub once, then install"
else report dsh missing "no AI Hub or DSH Desktop profile and CLI"; fi
workbuddy_settings="$HOME/.workbuddy/settings.json"
workbuddy_app=''
for candidate in '/Applications/WorkBuddy.app' "$HOME/Applications/WorkBuddy.app"; do
    if [ -f "$candidate/Contents/Info.plist" ]; then workbuddy_app=$candidate; break; fi
done
if [ ! -f "$workbuddy_settings" ]; then report workbuddy-desktop missing "no $workbuddy_settings"
elif [ -z "$workbuddy_app" ]; then report workbuddy-desktop missing "WorkBuddy settings found, but the WorkBuddy app is not installed"
else report workbuddy-desktop found "$workbuddy_settings"; hosts+=(workbuddy-desktop); fi
printf 'HOSTS=%s\n' "${hosts[*]-}"
