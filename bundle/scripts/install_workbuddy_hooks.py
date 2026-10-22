#!/usr/bin/env python3
"""Own only Observer's three native WorkBuddy tool hooks and their receipt."""
from __future__ import annotations

import argparse
import copy
import json
import os
import shlex
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

LEGACY_PLUGIN = 'skill-mcp-observer@aihub-local'
STAGES = {'PreToolUse': 'started', 'PostToolUse': 'completed', 'PostToolUseFailure': 'failed'}
# Other WorkBuddy builds that keep their own data folder but run the same hooks.
# WorkBuddy AI (international build): ~/.workbuddy-ai on macOS (seen 2026-10-22).
COMPANIONS = {'workbuddy-ai': '.workbuddy-ai'}


def _path(path: Path) -> Path:
    path = path.expanduser().absolute()
    if any(c in str(path) for c in '\x00\r\n'):
        raise ValueError('hook paths must not contain control characters')
    # 先保留原始路径的链接检查，再规范化 ..；两种拼写都不能绕过链接限制。
    normalized = Path(os.path.abspath(path))
    for spelling in (path, normalized):
        for candidate in (spelling, *spelling.parents):
            if candidate.is_symlink() or (hasattr(candidate, 'is_junction') and candidate.is_junction()):
                raise ValueError(f'hook path must not use links: {candidate}')
    return normalized.resolve()


def _distinct_paths(*paths: Path) -> None:
    # 实际目的文件必须不同；已存在的硬链接也不是独立的客户文件。
    canonical = [path.resolve() for path in paths]
    if len(set(canonical)) != len(paths):
        raise ValueError('hook settings, receipt, executable, bridge and source must be distinct')
    for index, path in enumerate(paths):
        if path.exists():
            for other in paths[index + 1:]:
                if other.exists() and path.samefile(other):
                    raise ValueError('hook settings, receipt, executable, bridge and source must be distinct')


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(raw, dict):
        raise ValueError(f'WorkBuddy settings/receipt must be an object: {path}')
    return raw


_WINDOWS = os.name == 'nt'
_REPLACE_RETRY_SECONDS = 15 if _WINDOWS else 0


def _replace(temp: Path, path: Path) -> None:
    """os.replace, retried on Windows while another process briefly holds the target.

    WorkBuddy re-reads settings.json while it runs hooks (and when the installer is
    started from inside WorkBuddy that happens constantly); Windows then refuses
    the rename with WinError 5 / 32 for a moment.
    """
    deadline = time.monotonic() + _REPLACE_RETRY_SECONDS
    while True:
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.25)


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    try:
        if json.loads(path.read_text(encoding='utf-8')) == value:
            # Same settings (WorkBuddy may format the file differently): do not
            # touch a file the client is reading, e.g. when installing from inside WorkBuddy.
            return
    except (FileNotFoundError, ValueError):
        pass
    fd, name = tempfile.mkstemp(prefix='.observer-hooks-', dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        try:
            _replace(temp, path)
        except PermissionError:
            if not _WINDOWS or not path.exists():
                raise
            # WorkBuddy holds settings.json open without delete sharing while it
            # runs (installing from inside WorkBuddy). Renaming over it is then
            # refused, but rewriting it in place is allowed; the caller keeps
            # the previous bytes for rollback.
            with open(path, 'r+b') as handle:
                handle.write(data); handle.truncate(); handle.flush(); os.fsync(handle.fileno())
    finally:
        if temp.exists():
            temp.unlink()


def policy_paths(settings: Path, supplied: tuple[Path, ...] = ()) -> tuple[Path, ...]:
    candidates = [*supplied]
    settings_parent = settings.expanduser().absolute().parent
    config_dir = settings_parent.name
    if config_dir not in {".workbuddy", ".workbuddy-ai", ".codebuddy"}:
        return tuple(dict.fromkeys(candidates))
    for root in (Path.cwd(), *Path.cwd().parents):
        for name in ("settings.json", "settings.local.json"):
            path = root / config_dir / name
            if path.exists() and path.absolute() != settings.expanduser().absolute():
                candidates.append(path)
    return tuple(dict.fromkeys(candidates))


def _policy(settings: Path, value: dict[str, Any], policy_settings: tuple[Path, ...]) -> None:
    # 用户与项目安全策略只能读取，不允许安装器替用户绕过。
    sources = [(settings, value)]
    for path in policy_paths(settings, policy_settings):
        policy = _path(path)
        if policy != settings:
            raw = _read(policy)
            _conflicts(raw)
            if raw.get('enabledPlugins', {}).get(LEGACY_PLUGIN) is True:
                raise ValueError(f'Observer plugin enabled in another scope: {policy}')
            sources.append((policy, raw))
    for path, raw in sources:
        if raw.get('disableAllHooks') or raw.get('allowManagedHooksOnly'):
            raise ValueError(f'WorkBuddy hook policy blocks user hooks: {path}')


def _remove_owned(raw: dict[str, Any], owned: dict[str, Any]) -> None:
    hooks = raw.get('hooks', {})
    if not isinstance(hooks, dict):
        raise ValueError('WorkBuddy hooks must be an object')
    for event, entry in owned['entries'].items():
        groups = hooks.get(event, [])
        if not isinstance(groups, list) or groups.count(entry) != 1:
            raise ValueError(f'Observer owned hook was edited or duplicated: {event}')
    plugins = raw.get('enabledPlugins', {})
    if owned['plugin_changed'] and plugins.get(LEGACY_PLUGIN) is not False:
        raise ValueError('Observer tracked plugin enablement was edited')
    for event, entry in owned['entries'].items():
        hooks[event].remove(entry)
        if not hooks[event]:
            del hooks[event]
    if not hooks and not owned['hooks_existed']:
        raw.pop('hooks', None)
    if owned['plugin_changed']:
        plugins[LEGACY_PLUGIN] = owned['plugin_previous']


def _conflicts(raw: dict[str, Any]) -> None:
    plugins = raw.get('enabledPlugins', {})
    if not isinstance(plugins, dict):
        raise ValueError('enabledPlugins must be an object')
    for identity, enabled in plugins.items():
        if identity.split('@', 1)[0] == 'skill-mcp-observer' and identity != LEGACY_PLUGIN and enabled:
            raise ValueError(f'Unknown Observer plugin ownership: {identity}')
    hooks = raw.get('hooks', {})
    if not isinstance(hooks, dict):
        raise ValueError('hooks must be an object')
    for groups in hooks.values():
        if not isinstance(groups, list):
            raise ValueError('native hook event entries must be arrays')
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get('hooks'), list):
                raise ValueError('native hook groups must contain hooks arrays')
            for hook in group['hooks']:
                if not isinstance(hook, dict):
                    raise ValueError('native hook entries must be objects')
                command = hook.get('command', '')
                if isinstance(command, str) and ('workbuddy_hook_bridge.py' in command or 'skill-mcp-observer' in command):
                    raise ValueError('Observer source has no matching ownership receipt')


def hook_command(argv: list[str], *, windows: bool | None = None) -> str:
    """Build the hook command line WorkBuddy runs.

    WorkBuddy on Windows runs hook commands through its bundled Git Bash, where
    unquoted backslashes are escape characters (C:\\Users\\x becomes C:Usersx and
    the hook silently never runs). Windows paths are therefore written with
    forward slashes, and double-quoted only when they contain bash-special
    characters; Windows, Python and bash all accept that spelling.
    """
    windows = os.name == 'nt' if windows is None else windows
    if not windows:
        return shlex.join(argv)
    parts = []
    for value in argv:
        value = value.replace('\\', '/')
        parts.append(f'"{value}"' if any(c in value for c in " \t'()[]{};&|<>*?#~=") else value)
    return ' '.join(parts)


def snapshot(*, settings: Path, receipt: Path) -> dict[Path, bytes | None]:
    settings, receipt = map(_path, (settings, receipt))
    _distinct_paths(settings, receipt)
    return {path: path.read_bytes() if path.exists() else None for path in (settings, receipt)}


def restore(previous: dict[Path, bytes | None], expected: dict[Path, bytes | None]) -> None:
    # 回滚前验证仍是本次写入；并发用户编辑必须可见，不能覆盖。
    for path, contents in expected.items():
        current = path.read_bytes() if path.exists() else None
        if current != contents:
            raise ValueError(f'WorkBuddy rollback blocked by concurrent edit: {path}')
    for path, contents in previous.items():
        if contents is None:
            if path.exists():
                path.unlink()
        else:
            if path.exists() and path.read_bytes() == contents:
                continue
            fd, name = tempfile.mkstemp(prefix='.observer-restore-', dir=path.parent)
            try:
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(contents); handle.flush(); os.fsync(handle.fileno())
                _replace(Path(name), path)
            finally:
                if os.path.exists(name):
                    os.unlink(name)


def install(*, settings: Path, receipt: Path, python: Path, bridge: Path, event_file: Path,
    policy_settings: tuple[Path, ...] = (), client: str = 'workbuddy') -> dict[str, Any]:
    if client != 'workbuddy' and client not in COMPANIONS:
        raise ValueError(f'unknown WorkBuddy client: {client}')
    settings, receipt, event_file = map(_path, (settings, receipt, event_file))
    python, bridge = python.expanduser().absolute(), bridge.expanduser().absolute()
    forbidden = '\x00\r\n' + ('&|<>^%!\"$`' if os.name == 'nt' else '')
    if any(c in str(path) for path in (python, bridge, event_file) for c in forbidden):
        raise ValueError('executable paths contain control characters')
    _distinct_paths(settings, receipt, python, bridge, event_file)
    if not python.is_file() or not bridge.is_file():
        raise FileNotFoundError('managed Python or packaged WorkBuddy bridge is unavailable')
    raw = _read(settings); before = snapshot(settings=settings, receipt=receipt)
    _policy(settings, raw, policy_settings)
    owned = _read(receipt)
    if owned:
        if owned.get('schema') != 'observer.workbuddy.hooks.v1' or owned.get('settings') != str(settings):
            raise ValueError('Unknown Observer hook ownership receipt')
        _remove_owned(raw, owned)
    _conflicts(raw)
    hooks_existed = 'hooks' in raw
    hooks = raw.setdefault('hooks', {})
    entries = {}
    for event, stage in STAGES.items():
        argv = [str(python), str(bridge), '--stage', stage, '--event-file', str(event_file)]
        if client != 'workbuddy':
            argv += ['--client', client]
        command = hook_command(argv)
        entry = {'matcher': '.*', 'hooks': [{'type': 'command', 'command': command, 'timeout': 5}]}
        entries[event] = entry
        hooks.setdefault(event, []).append(copy.deepcopy(entry))
    plugins = raw.get('enabledPlugins', {})
    plugin_previous = plugins.get(LEGACY_PLUGIN)
    plugin_changed = plugin_previous is True
    if plugin_changed:
        plugins[LEGACY_PLUGIN] = False
    if event_file.exists():
        if not event_file.is_file():
            raise ValueError('native hook source is not a regular file')
    else:
        event_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(event_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    os.chmod(event_file, 0o600)
    record = {'schema': 'observer.workbuddy.hooks.v1', 'settings': str(settings), 'entries': entries,
        'hooks_existed': hooks_existed, 'plugin_changed': plugin_changed, 'plugin_previous': plugin_previous,
        'event_file': str(event_file)}
    try:
        _write(settings, raw)
        _write(receipt, record)
    except BaseException:
        restore(before, snapshot(settings=settings, receipt=receipt))
        raise
    return {'status': 'ok', 'hooks_installed': list(entries), 'native_event_acceptance': 'unverified'}


def uninstall(*, settings: Path, receipt: Path) -> dict[str, Any]:
    settings, receipt = map(_path, (settings, receipt))
    _distinct_paths(settings, receipt)
    owned = _read(receipt)
    raw = _read(settings)
    if not owned:
        _conflicts(raw)
        return {'status': 'ok', 'changed': False, 'source_preserved': True}
    if owned.get('schema') != 'observer.workbuddy.hooks.v1' or owned.get('settings') != str(settings):
        raise ValueError('Unknown Observer hook ownership receipt')
    _remove_owned(raw, owned)
    before = snapshot(settings=settings, receipt=receipt)
    try:
        _write(settings, raw); receipt.unlink()
    except BaseException:
        restore(before, snapshot(settings=settings, receipt=receipt))
        raise
    return {'status': 'ok', 'changed': True, 'source_preserved': True}


def status(*, settings: Path, receipt: Path, policy_settings: tuple[Path, ...] = ()) -> dict[str, Any]:
    settings, receipt = map(_path, (settings, receipt))
    _distinct_paths(settings, receipt)
    raw = _read(settings); _policy(settings, raw, policy_settings)
    owned = _read(receipt)
    if not owned:
        raise ValueError('Observer native hook ownership receipt is missing')
    if owned.get('schema') != 'observer.workbuddy.hooks.v1' or owned.get('settings') != str(settings):
        raise ValueError('Unknown Observer hook ownership receipt')
    _remove_owned(raw, owned); _conflicts(raw)
    return {'status': 'ok', 'hooks_installed': list(owned['entries']), 'native_event_acceptance': 'unverified'}


def _main_hook_paths(main_receipt: Path) -> tuple[Path, Path, Path]:
    """Python, bridge and event file the main WorkBuddy hooks already run with."""
    owned = _read(_path(main_receipt))
    if owned.get('schema') != 'observer.workbuddy.hooks.v1':
        raise ValueError('WorkBuddy hooks are not installed by Observer')
    command = owned['entries']['PreToolUse']['hooks'][0]['command']
    argv = shlex.split(command, posix=True)
    if len(argv) < 2:
        raise ValueError('unexpected WorkBuddy hook command')
    return Path(argv[0]), Path(argv[1]), Path(owned['event_file'])


def companion_receipt(main_receipt: Path, client: str) -> Path:
    return main_receipt.parent / f'{client}-hooks-owned.json'


def sync_companions(*, main_receipt: Path, home: Path | None = None) -> dict[str, Any]:
    """Add Observer's hooks to WorkBuddy AI (and later builds) when they are installed.

    Runs after every WorkBuddy install and from the updater every 30 minutes, so a
    build installed after Observer is picked up without reinstalling. The hooks are
    the same as WorkBuddy's (same Python, bridge and event file) plus --client.
    A build that is not installed is left alone; a failure is reported, never raised.
    """
    home = Path.home() if home is None else home
    results: dict[str, Any] = {}
    for client, folder in COMPANIONS.items():
        settings = home / folder / 'settings.json'
        receipt = companion_receipt(main_receipt, client)
        if not settings.parent.is_dir():
            results[client] = {'status': 'not_installed'}
            continue
        try:
            python, bridge, event_file = _main_hook_paths(main_receipt)
            before = settings.read_bytes() if settings.exists() else None
            result = install(settings=settings, receipt=receipt, python=python, bridge=bridge,
                event_file=event_file, client=client)
            after = settings.read_bytes() if settings.exists() else None
            results[client] = {**result, 'changed': before != after}
        except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
            results[client] = {'status': 'blocked', 'error_type': type(error).__name__, 'reason': str(error)}
    return results


def uninstall_companions(*, main_receipt: Path, home: Path | None = None) -> dict[str, Any]:
    home = Path.home() if home is None else home
    results: dict[str, Any] = {}
    for client, folder in COMPANIONS.items():
        receipt = companion_receipt(main_receipt, client)
        if not receipt.exists():
            continue
        try:
            results[client] = uninstall(settings=home / folder / 'settings.json', receipt=receipt)
        except (OSError, ValueError, KeyError, TypeError) as error:
            results[client] = {'status': 'blocked', 'error_type': type(error).__name__, 'reason': str(error)}
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('install','status','uninstall'))
    parser.add_argument('--settings', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--policy-settings', type=Path, action='append', default=[])
    for name in ('python','bridge','event-file'):
        parser.add_argument('--'+name, type=Path)
    args = parser.parse_args(argv)
    try:
        options = {'settings': args.settings, 'receipt': args.receipt}
        if args.command != 'uninstall':
            options['policy_settings'] = tuple(args.policy_settings)
        if args.command == 'install':
            if any(getattr(args, name) is None for name in ('python','bridge','event_file')):
                raise ValueError('install requires managed --python, --bridge and --event-file')
            options.update(python=args.python, bridge=args.bridge, event_file=args.event_file)
        result = globals()[args.command](**options)
        sys.stdout.write(json.dumps(result)+'\n'); return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        sys.stdout.write(json.dumps({'status':'blocked','error_type':type(error).__name__,'reason':str(error)})+'\n'); return 2


if __name__ == '__main__':
    raise SystemExit(main())
