#!/bin/bash
# 仅用 macOS 自带工具在 Python 下载前检查；不会创建目录或读取私有文件内容。
observer_canonical_path() {
    local value=$1 depth=${2:-0} resolve=${3:-yes} current='/' component next target
    local parts=()
    [ "$depth" -lt 40 ] || fail 'layout path contains a symbolic-link cycle'
    case "$value" in *$'\n'*|*$'\r'*) fail 'layout path contains control characters' ;; esac
    case "$value" in \~*) fail 'layout paths require an expanded home directory' ;; esac
    case "$value" in /*) ;; *) value="$PWD/$value" ;; esac
    IFS=/ read -r -a parts <<< "$value"
    for component in "${parts[@]}"; do
        case "$component" in ''|.) continue ;; ..) current=$(dirname "$current"); continue ;; esac
        next="${current%/}/$component"
        if [ "$resolve" = yes ] && [ -d "$next" ]; then
            # Bash 的 pwd 保留用户大小写；系统 pwd 使用真实目录身份返回物理拼写。
            current=$(cd "$next" && /bin/pwd -P) || return
        elif [ "$resolve" = yes ] && [ -L "$next" ]; then
            target=$(/usr/bin/readlink "$next") || return
            case "$target" in /*) ;; *) target="${current%/}/$target" ;; esac
            current=$(observer_canonical_path "$target" "$((depth + 1))") || return
        else
            current=$next
        fi
    done
    printf '%s\n' "$current"
}

observer_assert_layout() {
    local bundle path candidate alias root suffix canonical lexical
    local roots=()
    bundle=$(observer_canonical_path "$1") || return
    shift
    roots=("$bundle")
    for suffix in previous replacement backup rollback-replacement rollback-backup; do
        roots+=("$(dirname "$bundle")/.$(basename "$bundle").$suffix")
    done
    for path in "$@"; do
        [ -n "$path" ] || continue
        canonical=$(observer_canonical_path "$path") || return
        lexical=$(observer_canonical_path "$path" 0 no) || return
        for root in "${roots[@]}"; do
            for candidate in "$canonical" "$lexical"; do
                case "$candidate/" in "$root/"*) fail 'bundle overlap with protected runtime, private or selected native path' ;; esac
                case "$root/" in "$candidate/"*) fail 'bundle overlap with protected runtime, private or selected native path' ;; esac
            done
            if [ -f "$path" ] && [ -d "$root" ]; then
                alias=$(/usr/bin/find "$root" -type f -samefile "$path" -print -quit) || return
                [ -z "$alias" ] || fail 'bundle overlap with protected hardlinked file'
            fi
        done
    done
}

observer_validate_bootstrap_layout() {
    local bundle="$HOME/Library/Application Support/AIHub/observer/bundle" host option value state source lock plist
    local dsh_source="$HOME/.aihub/observer/dsh" dsh_state="$HOME/Library/Application Support/AIHub/dsh-observer"
    local dsh_profile="$HOME/Library/Application Support/dsh-desktop/harness/profiles/web" dsh_cli='/Applications/DSH Desktop.app/Contents/Resources/app/node_modules/@deepseek-ai/dsh/lib/bin.js'
    local codex_source="$HOME/.codex/sessions" codex_state="$HOME/Library/Application Support/AIHub/observer"
    local claude_source="$HOME/Library/Application Support/Claude/IndexedDB/https_claude.ai_0.indexeddb.leveldb" claude_state="$HOME/Library/Application Support/AIHub/claude-observer"
    local workbuddy_source="$HOME/.aihub/observer/workbuddy-hook-events.jsonl" workbuddy_state="$HOME/Library/Application Support/AIHub/workbuddy-observer" workbuddy_settings="$HOME/.workbuddy/settings.json"
    local agent_dir='' protected=("$observer_runtime" "$observer_config" "${observer_credentials:-}" "$observer_archive") policies=()
    while [ "$#" -gt 0 ]; do
        option=$1; shift
        case "$option" in
            --install-root|--launch-agent-dir|--dsh-source|--dsh-state-dir|--dsh-profile-dir|--dsh-cli|--codex-source|--codex-state-dir|--claude-source|--claude-state-dir|--workbuddy-source|--workbuddy-state-dir|--workbuddy-settings|--workbuddy-policy-settings)
                [ "$#" -gt 0 ] || fail 'layout option requires a value'; value=$1; shift ;;
            --*=*) value=${option#*=}; option=${option%%=*} ;;
            *) continue ;;
        esac
        case "$option" in
            --install-root) bundle=$value ;; --launch-agent-dir) agent_dir=$value ;;
            --dsh-source) dsh_source=$value ;; --dsh-state-dir) dsh_state=$value ;; --dsh-profile-dir) dsh_profile=$value ;; --dsh-cli) dsh_cli=$value ;;
            --codex-source) codex_source=$value ;; --codex-state-dir) codex_state=$value ;;
            --claude-source) claude_source=$value ;; --claude-state-dir) claude_state=$value ;;
            --workbuddy-source) workbuddy_source=$value ;; --workbuddy-state-dir) workbuddy_state=$value ;; --workbuddy-settings) workbuddy_settings=$value ;; --workbuddy-policy-settings) policies+=("$value") ;;
        esac
    done
    for host in "${observer_hosts[@]}"; do
        case "$host" in
            dsh) source=$dsh_source; state=$dsh_state; lock=dsh; plist=com.observer.dsh-observer; protected+=("$dsh_profile" "$dsh_cli") ;;
            codex) source=$codex_source; state=$codex_state; lock=codex; plist=com.aihub.codex-observer ;;
            claude) source=$claude_source; state=$claude_state; lock=claude; plist=com.aihub.claude-observer ;;
            workbuddy-desktop) source=$workbuddy_source; state=$workbuddy_state; lock=workbuddy; plist=com.aihub.workbuddy-observer; protected+=("$workbuddy_settings" "$state/workbuddy-hooks-owned.json" ${policies[@]+"${policies[@]}"}) ;;
        esac
        protected+=("$source" "$state/observer.sqlite3" "$state/observer.sqlite3-wal" "$state/observer.sqlite3-shm" "$state/langfuse-content-outbox.sqlite3" "$state/langfuse-content-outbox.sqlite3-wal" "$state/langfuse-content-outbox.sqlite3-shm" "$state/status.json" "$state/installation.json" "$state/$lock-observer.lock")
        if [ -n "$agent_dir" ]; then protected+=("$agent_dir/$host.plist"); else protected+=("$HOME/Library/LaunchAgents/$plist.plist"); fi
    done
    observer_assert_layout "$bundle" "${protected[@]}"
}
