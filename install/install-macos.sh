#!/bin/bash
# Observer 安装（macOS）。不需要 AI 助手：双击 Install-Observer-macOS.command，或在“终端”里运行
#   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/LoctekIT-AI/observer/main/install/install-macos.sh)"
# 步骤：读取签名的版本文件 → 下载安装包并校验 SHA-256 → 检测本机客户端 → 征得同意后安装（飞书登录一次）。
set -euo pipefail
REPO=LoctekIT-AI/observer
# 默认安装正式版（release）；测试环境验收时用 OBSERVER_CHANNEL=test。
CHANNEL=${OBSERVER_CHANNEL:-release}
fail() { printf '\n安装没有完成：%s\n' "$1" >&2; exit 1; }
ask() { local answer=''; if [ "${OBSERVER_ASSUME_YES:-}" = 1 ]; then answer=Y; else read -r -p "$1" answer < /dev/tty || true; fi; printf '%s' "$answer"; }

printf '\n==== Observer 安装 ====\n'

work=$(/usr/bin/mktemp -d /private/tmp/observer-install.XXXXXX)
channel_url="https://raw.githubusercontent.com/$REPO/main/channels/$CHANNEL.json"
printf '读取最新版本：%s\n' "$channel_url"
/usr/bin/curl -fsSL --proto '=https' "$channel_url" -o "$work/channel.json" || fail '无法读取版本文件'
field() { /usr/bin/plutil -extract "$1" raw -o - "$work/channel.json"; }
version=$(field version); tag=$(field tag); asset=$(field asset); sha=$(field sha256); size=$(field size)
suffix=''; [ "$CHANNEL" = test ] && suffix='-test'
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail '版本文件格式不对'
[ "$tag" = "v$version$suffix" ] && [ "$asset" = "observer-v$version$suffix.zip" ] && [[ "$sha" =~ ^[0-9a-f]{64}$ ]] || fail '版本文件格式不对'
printf '最新版本：%s\n' "$tag"

archives="$HOME/Library/Application Support/AIHub/observer/archives"
mkdir -p "$archives"
archive="$archives/$asset"
url="https://github.com/$REPO/releases/download/$tag/$asset"
printf '下载安装包：%s\n' "$url"
/usr/bin/curl -fL --proto '=https' --proto-redir '=https' --silent --show-error "$url" -o "$archive" || fail '下载失败'
actual=$(/usr/bin/shasum -a 256 "$archive"); actual=${actual%% *}
[ "$actual" = "$sha" ] && [ "$(/usr/bin/stat -f %z "$archive")" = "$size" ] || { rm -f "$archive"; fail '安装包校验失败（SHA-256 不一致）'; }
printf '安装包校验通过。\n'
/usr/bin/ditto -x -k "$archive" "$work/source"

printf '\n检测本机的 AI 客户端：\n'
detect=$(/bin/bash "$work/source/scripts/observer_detect_clients_macos.sh")
printf '%s\n' "$detect" | /usr/bin/grep -v '^HOSTS=' | /usr/bin/sed 's/^/  /'
found=$(printf '%s\n' "$detect" | /usr/bin/sed -n 's/^HOSTS=//p' | tail -1)
[ -n "$found" ] || fail '没有检测到支持的客户端（Codex、Claude、AI Hub、WorkBuddy）。AI Hub 需要先打开过一次。'

printf '\n将为这些客户端安装：%s\n' "$found"
answer=$(ask '全部安装请输入 Y；只装其中几个请输入名称（用空格分隔）；直接回车取消：')
[ -n "$answer" ] || { printf '已取消，没有做任何改动。\n'; exit 0; }
selected=$found
if [ "$answer" != Y ] && [ "$answer" != y ]; then
    selected=$(printf '%s' "$answer" | /usr/bin/sed -e 's/，/ /g' -e 's/,/ /g')
    for name in $selected; do case " $found " in *" $name "*) ;; *) fail "没有检测到客户端：$name" ;; esac; done
fi
args=(); for name in $selected; do args+=(--host "$name"); done

# 记下安装前本机有哪些 WorkBuddy（安装会写钩子，之后就分不清了），结尾只提示真正装了的客户端。
had_workbuddy=0; [ -f "$HOME/.workbuddy/settings.json" ] && had_workbuddy=1
had_workbuddy_ai=0; [ -f "$HOME/.workbuddy-ai/settings.json" ] && had_workbuddy_ai=1
printf '\n开始安装。第一次安装会打开浏览器，请用飞书登录；之后不会再要求登录。\n'
result="$work/result.tsv"; : > "$result"
OBSERVER_RESULT_FILE=$result /bin/bash "$work/source/scripts/bootstrap_observer_macos.sh" --archive "$archive" "${args[@]}" --authorize || fail '安装程序出错，请把上面的输出发给 AI 平台团队'
skipped=$(/usr/bin/awk -F '\t' '$1 == "skipped" { print $2 }' "$result" | /usr/bin/tr '\n' ' ')
noted=$(/usr/bin/awk -F '\t' '$1 == "note" { print $2 }' "$result" | /usr/bin/tr '\n' ' ')
installed=''
for name in $selected; do case " $skipped " in *" $name "*) ;; *) installed="$installed $name" ;; esac; done
printf '\n安装完成：%s。之后采集和上报都在后台自动进行，也会自动更新。\n' "${installed# }"
[ -z "$skipped" ] || printf '已跳过（客户端版本太旧，Observer 还不支持）：%s。其他客户端不受影响；升级这个客户端后再双击安装一次即可。\n' "${skipped% }"
[ -z "$noted" ] || printf '提示：%s 的版本比测试过的新（或读不到版本），已照常安装；读不懂的调用会单独统计，不影响使用。\n' "${noted% }"
restart=''
case " $installed " in *" dsh "*) restart='AI Hub' ;; esac
case " $installed " in *" workbuddy-desktop "*)
    [ "$had_workbuddy" = 1 ] && restart="${restart:+$restart、}WorkBuddy"
    [ "$had_workbuddy_ai" = 1 ] && restart="${restart:+$restart、}WorkBuddy AI" ;; esac
[ -z "$restart" ] || printf '请把 %s 完全退出（Cmd+Q）再打开一次。\n' "$restart"
