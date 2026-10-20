#!/bin/bash
# 在专属目录准备真实运行环境；所有原生命令失败立即可见。
set -euo pipefail

fail() { printf 'Observer bootstrap: %s\n' "$1" >&2; exit 2; }
usage() {
    printf '%s\n' 'Usage: bootstrap_observer_macos.sh --archive PATH --host HOST [--config-file PATH] [--runtime-root PATH] [--authorize [--auth-origin URL] [--replace]] [-- INSTALLER_ARGS...]'
}
observer_archive=''
observer_hosts=()
observer_runtime="${HOME}/Library/Application Support/AIHub/observer/runtime"
observer_config="${HOME}/Library/Application Support/AIHub/observer/customer.env"
observer_authorize=0
observer_auth_origin='https://itsub.loctek.com/observer'
observer_replace=()
observer_installer_args=()
while [ "$#" -gt 0 ]; do
    case "$1" in
        --archive|--host|--config-file|--runtime-root|--auth-origin)
            [ "$#" -ge 2 ] || fail "option requires a value"
            case "$1" in
                --archive) observer_archive=$2 ;;
                --host) case "$2" in dsh|codex|claude|workbuddy-desktop) observer_hosts+=("$2") ;; *) fail 'unsupported host' ;; esac ;;
                --config-file) observer_config=$2 ;;
                --runtime-root) observer_runtime=$2 ;;
                --auth-origin) observer_auth_origin=$2 ;;
            esac
            shift 2 ;;
        --authorize) observer_authorize=1; shift ;;
        --replace) observer_replace=(--replace); shift ;;
        --help|-h) usage; exit 0 ;;
        --) shift; observer_installer_args=("$@"); break ;;
        *) fail 'unknown bootstrap option' ;;
    esac
done
[ -n "$observer_archive" ] || fail '--archive is required'
[ "${#observer_hosts[@]}" -gt 0 ] || fail '--host is required'
[ -f "$observer_archive" ] || fail 'archive does not exist'
[ "$(/usr/bin/uname -s)" = Darwin ] || fail 'macOS is required'
case "$(/usr/bin/uname -m)" in
    arm64) observer_uv_arch=aarch64; observer_node_arch=arm64 ;;
    x86_64) observer_uv_arch=x86_64; observer_node_arch=x64 ;;
    *) fail 'unsupported macOS architecture' ;;
esac
if [ "$observer_authorize" -eq 0 ]; then
    [ "${#observer_replace[@]}" -eq 0 ] || fail '--replace requires --authorize'
    [ -f "$observer_config" ] || fail 'configuration is missing; use --authorize for company-site Observer authorization'
fi
for observer_arg in ${observer_installer_args[@]+"${observer_installer_args[@]}"}; do
    case "$observer_arg" in --host|--host=*|--python|--python=*|--config-file|--config-file=*|--runtime-root|--runtime-root=*) fail 'installer arguments may not override bootstrap host, Python or config' ;; esac
done
observer_source=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
observer_requirements="$observer_source/packages/plugin-bundle/requirements-observer.txt"
observer_installer="$observer_source/scripts/install_observer_bundle.py"
[ -f "$observer_requirements" ] && [ -f "$observer_installer" ] || fail 'extracted Observer source is incomplete'
# 必须先拒绝会被归档替换/删除的运行时和选中端私有路径。
source "$observer_source/scripts/observer_macos_layout.sh"
observer_validate_bootstrap_layout ${observer_installer_args[@]+"${observer_installer_args[@]}"}
# 本地固定摘要文件随源码交付，不下载远端脚本执行。
source "$observer_source/packages/plugin-bundle/observer-runtime-pins.env"
if [ "$observer_node_arch" = arm64 ]; then observer_uv_hash=$UV_ARM64_SHA256; observer_node_hash=$NODE_ARM64_SHA256
else observer_uv_hash=$UV_X64_SHA256; observer_node_hash=$NODE_X64_SHA256; fi
mkdir -p "$observer_runtime"
observer_runtime=$(cd "$observer_runtime" && pwd -P)
mkdir -p "$observer_runtime/downloads" "$observer_runtime/bin"
export UV_PYTHON_INSTALL_DIR="$observer_runtime/python"
export UV_CACHE_DIR="$observer_runtime/cache"
unset UV_PYTHON_PREFERENCE UV_VENV_CLEAR PYTHONPATH PYTHONHOME VIRTUAL_ENV CONDA_PREFIX
export UV_NO_CONFIG=1
# 只继承系统工具路径，确保不依赖开发机全局 Python、Node 或 pnpm。
export PATH='/usr/bin:/bin:/usr/sbin:/sbin'

verified_download() {
    local url=$1 target=$2 digest=$3 actual temporary
    if [ ! -f "$target" ]; then
        temporary=$(/usr/bin/mktemp "$observer_runtime/downloads/.download.XXXXXX")
        /usr/bin/curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --tlsv1.2 "$url" --output "$temporary"
        actual=$(/usr/bin/shasum -a 256 "$temporary"); actual=${actual%% *}
        [ "$actual" = "$digest" ] || fail 'download SHA256 does not match pinned upstream metadata'
        /bin/mv "$temporary" "$target"
    fi
    actual=$(/usr/bin/shasum -a 256 "$target"); actual=${actual%% *}
    [ "$actual" = "$digest" ] || fail 'cached download SHA256 does not match pinned upstream metadata'
}
observer_uv_asset="uv-${observer_uv_arch}-apple-darwin.tar.gz"
verified_download "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${observer_uv_asset}" "$observer_runtime/downloads/$observer_uv_asset" "$observer_uv_hash"
/usr/bin/tar -xzf "$observer_runtime/downloads/$observer_uv_asset" -C "$observer_runtime/bin"
observer_uv="$observer_runtime/bin/uv-${observer_uv_arch}-apple-darwin/uv"
"$observer_uv" --version
"$observer_uv" venv --python 3.12 --managed-python --allow-existing "$observer_runtime/venv"
"$observer_uv" pip install --python "$observer_runtime/venv/bin/python" -r "$observer_requirements"
observer_python="$observer_runtime/venv/bin/python"
# 实际导入且逐项比对已安装版本，不把 pip 退出成功当作运行时验收。
"$observer_python" - "$observer_requirements" "$observer_source" <<'PY'
import importlib.metadata
import json
import sys
from pathlib import Path
requirements = Path(sys.argv[1]).read_text().splitlines()
versions = {}
for line in requirements:
    if not line or line.startswith('#'):
        continue
    name, expected = line.split('==', 1)
    actual = importlib.metadata.version(name)
    if actual != expected:
        raise RuntimeError('Observer dependency version mismatch: ' + name)
    versions[name] = actual
if sys.version_info[:2] != (3, 12):
    raise RuntimeError('Observer requires managed Python 3.12')
source = Path(sys.argv[2])
sys.path[:0] = [str(source / 'packages/contracts/src'), str(source / 'apps/cowork-runtime/src')]
import pydantic
import yaml
import cowork_contracts
import cowork_runtime.plugin_package
from cowork_runtime.telemetry.langfuse import LangfuseConfig
from cowork_runtime.observer.dsh_worker import DshObserverWorker
sys.stdout.write(json.dumps({'runtime': 'verified', 'python': sys.version.split()[0], 'dependencies': versions}, sort_keys=True) + '\n')
PY
# Version check per client before installing (application versions only). A client too old
# for this collector is skipped and the others are installed; a newer, untested version, or one
# whose version cannot be read, is installed with a note. If OBSERVER_RESULT_FILE is set, one line
# per skipped / noted client is appended to it (<skipped|note> TAB <host> TAB <reason>).
# Custom test layouts are left to the doctor that runs after the install.
observer_result_file=${OBSERVER_RESULT_FILE:-}
case " ${observer_installer_args[*]-} " in
    *" --install-root"*|*" --launch-agent-dir"*) ;;
    *)
        observer_doctor_args=()
        for observer_host in "${observer_hosts[@]}"; do observer_doctor_args+=(--host "$observer_host"); done
        if observer_checks=$("$observer_python" "$observer_source/scripts/observer_doctor.py" --compatibility-only "${observer_doctor_args[@]}" 2>/dev/null); then
            observer_kept=()
            while IFS=$'\t' read -r observer_check_host observer_check_state observer_check_reason; do
                [ -n "$observer_check_host" ] || continue
                case "$observer_check_state" in
                    supported) observer_kept+=("$observer_check_host") ;;
                    unsupported)
                        printf 'Observer bootstrap: skipped %s: %s\n' "$observer_check_host" "$observer_check_reason"
                        [ -z "$observer_result_file" ] || printf 'skipped\t%s\t%s\n' "$observer_check_host" "$observer_check_reason" >> "$observer_result_file" ;;
                    *)
                        observer_kept+=("$observer_check_host")
                        printf 'Observer bootstrap: note: %s: %s\n' "$observer_check_host" "$observer_check_reason"
                        [ -z "$observer_result_file" ] || printf 'note\t%s\t%s\n' "$observer_check_host" "$observer_check_reason" >> "$observer_result_file" ;;
                esac
            done <<< "$observer_checks"
            [ "${#observer_kept[@]}" -gt 0 ] || fail 'none of the selected clients has a version this collector supports'
            observer_hosts=("${observer_kept[@]}")
        fi ;;
esac
observer_dsh=0
observer_host_args=()
for observer_host in "${observer_hosts[@]}"; do
    observer_host_args+=(--host "$observer_host")
    if [ "$observer_host" = dsh ]; then observer_dsh=1; fi
done
if [ "$observer_dsh" -eq 1 ]; then
    observer_node_asset="node-v${NODE_VERSION}-darwin-${observer_node_arch}.tar.gz"
    verified_download "https://nodejs.org/dist/v${NODE_VERSION}/${observer_node_asset}" "$observer_runtime/downloads/$observer_node_asset" "$observer_node_hash"
    /usr/bin/tar -xzf "$observer_runtime/downloads/$observer_node_asset" -C "$observer_runtime/bin"
    observer_node_root="$observer_runtime/bin/node-v${NODE_VERSION}-darwin-${observer_node_arch}"
    observer_pnpm_root="$observer_runtime/pnpm"
    export PATH="$observer_node_root/bin:$observer_pnpm_root/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    [ "$("$observer_node_root/bin/node" --version)" = "v$NODE_VERSION" ] || fail 'managed Node version mismatch'
    mkdir -p "$observer_runtime/npm" "$observer_pnpm_root"
    # npm 的用户/全局配置显式隔离，原 profile 的 .npmrc 完全保留。
    : > "$observer_runtime/npm/user.npmrc"
    : > "$observer_runtime/npm/global.npmrc"
    observer_pnpm_archive="$observer_runtime/downloads/pnpm-${PNPM_VERSION}.tgz"
    if [ ! -f "$observer_pnpm_archive" ]; then
        /usr/bin/curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --tlsv1.2 "https://registry.npmjs.org/pnpm/-/pnpm-${PNPM_VERSION}.tgz" --output "$observer_pnpm_archive"
    fi
    observer_pnpm_hash=$(/usr/bin/openssl dgst -sha512 -binary "$observer_pnpm_archive" | /usr/bin/openssl base64 -A)
    [ "$observer_pnpm_hash" = "$PNPM_SHA512" ] || fail 'pnpm tarball integrity does not match pinned npm metadata'
    (
        cd "$observer_runtime/npm"
        "$observer_node_root/bin/npm" --userconfig "$observer_runtime/npm/user.npmrc" --globalconfig "$observer_runtime/npm/global.npmrc" --cache "$observer_runtime/npm/cache" install --global --prefix "$observer_pnpm_root" --ignore-scripts --registry=https://registry.npmjs.org "$observer_pnpm_archive"
    )
    [ "$("$observer_pnpm_root/bin/pnpm" --version)" = "$PNPM_VERSION" ] || fail 'managed pnpm version mismatch'
fi
# Reuse an existing login only for the same authorization site: moving from the test
# environment to production (another origin) must log in again.
if [ "$observer_authorize" -eq 1 ] && [ "${#observer_replace[@]}" -eq 0 ] && [ -f "$observer_config" ] && /usr/bin/grep -q '^OBSERVER_AUTH_TOKEN_REF=.' "$observer_config" \
    && /usr/bin/grep -qxF "OBSERVER_AUTH_ORIGIN=${observer_auth_origin%/}" "$observer_config"; then
    printf '%s\n' 'Observer bootstrap: existing Observer login found; reusing it without a new login'
elif [ "$observer_authorize" -eq 1 ]; then
    "$observer_python" "$observer_source/scripts/observer_authorize.py" --client-type "${observer_hosts[0]}" --config-file "$observer_config" --auth-origin "$observer_auth_origin" ${observer_replace[@]+"${observer_replace[@]}"}
fi
observer_install_rc=0
"$observer_python" "$observer_installer" install "$observer_archive" "${observer_host_args[@]}" --config-file "$observer_config" --python "$observer_python" --runtime-root "$observer_runtime" ${observer_installer_args[@]+"${observer_installer_args[@]}"} || observer_install_rc=$?
# The updater is registered even when a client failed, so a later fixed version can still arrive.
# Signed self-update (scripts/observer_update.py): one LaunchAgent for all clients.
# Custom test layouts manage their own agents and are left alone.
observer_updater="$HOME/Library/Application Support/AIHub/observer/bundle/scripts/observer_update.py"
case " ${observer_installer_args[*]-} " in
    *" --install-root"*|*" --launch-agent-dir"*) printf '%s\n' 'Observer bootstrap: custom layout; self-update LaunchAgent not registered' ;;
    *)
        if [ -f "$observer_updater" ]; then
            "$observer_python" "$observer_updater" register --python "$observer_python" || printf '%s\n' 'Observer bootstrap: warning: self-update LaunchAgent could not be registered' >&2
        fi ;;
esac
exit "$observer_install_rc"
