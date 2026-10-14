"""Strict incremental adapter for WorkBuddy's persisted project JSONL."""
from __future__ import annotations

import hashlib, json, re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from cowork_contracts import ComponentEventV1
from .registry import CodexCursor, PluginRegistry

class WorkBuddyJsonlParseError(ValueError): pass

@dataclass(frozen=True)
class WorkBuddyJsonlReadResult:
    events: tuple[ComponentEventV1, ...]
    next_cursor: CodexCursor
    diagnostics: tuple[str, ...] = ()

def _time(value: object) -> datetime:
    if isinstance(value, int) and not isinstance(value, bool):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    if not isinstance(value,str): raise ValueError("timestamp must be an RFC3339 string")
    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None: raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc)

def _required(record: dict[str,Any], key: str) -> str:
    value=record.get(key)
    if not isinstance(value,str) or not value.strip(): raise ValueError(f"{key} must be non-empty")
    return value.strip()

def _kind(name: str) -> tuple[str,str]:
    if name.startswith('mcp__'): return 'mcp_tool','mcp_call'
    if name.casefold()=='skill': return 'skill','skill_call'
    return 'tool','tool_call'

def _event_id(installation: str, session: str, call: str, stage: str) -> str:
    return 'workbuddy-jsonl:'+hashlib.sha256(f'{installation}:{session}:{call}:{stage}'.encode()).hexdigest()

def _skill_id(value: object) -> str | None:
    if not isinstance(value, str): return None
    match = re.search(r'[/\\]skills[/\\]([^/\\\s]+)', value)
    return match.group(1) if match else None

class WorkBuddyJsonlAdapter:
    def __init__(self, registry: PluginRegistry) -> None: self.registry=registry

    def read(self,path: str|Path,*,installation_id: str,session_id: str,run_id: str|None=None,now: datetime|None=None)->WorkBuddyJsonlReadResult:
        source=Path(path).expanduser().resolve()
        if not source.is_file(): raise WorkBuddyJsonlParseError(f"session JSONL is not a file: {source}")
        self.registry._require_active(installation_id); binding=self.registry.binding(installation_id)
        stat=source.stat(); identity=f'{stat.st_dev}:{stat.st_ino}'; cursor=self.registry.cursor(installation_id,str(source)); offset=cursor.offset if cursor else 0
        if cursor and cursor.file_identity!=identity: raise WorkBuddyJsonlParseError('session JSONL identity changed')
        if stat.st_size<offset: raise WorkBuddyJsonlParseError('session JSONL was truncated')
        starts: dict[str,tuple[str,datetime]]={}; events=[]
        try:
            with source.open('rb') as handle:
                while True:
                    line_offset=handle.tell(); raw=handle.readline()
                    if not raw: break
                    if not raw.endswith(b'\n'): raise ValueError('session JSONL has truncated final line')
                    record=json.loads(raw.decode())
                    if not isinstance(record,dict) or record.get('sessionId')!=session_id: continue
                    typ=record.get('type')
                    if typ=='message':
                        continue
                    if typ=='function_call':
                        call=_required(record,'callId'); name=_required(record,'name'); occurred=_time(record.get('timestamp')); starts[call]=(name,occurred)
                        if line_offset>=offset:
                            ck,ok=_kind(name); events.append(ComponentEventV1(schema_version='component_event.v1',event_id=_event_id(installation_id,session_id,call,'started'),invocation_id=call,run_id=run_id or session_id,runtime='workbuddy',plugin_id='workbuddy-desktop-adapter',component_kind=ck,component_id=name,stage='started',occurred_at=occurred,started_at=occurred,observation_kind=ok,evidence_source='workbuddy_project_jsonl',evidence_level='verified',installation_id=installation_id,tenant_id=binding.tenant_id if binding else None,user_id=binding.user_id if binding else None,source_type='workbuddy_project_jsonl'))
                        continue
                    if typ=='function_call_result':
                        call=_required(record,'callId'); name=_required(record,'name'); status=_required(record,'status'); occurred=_time(record.get('timestamp'))
                        if call not in starts: raise ValueError('function result has no preceding call')
                        if status not in {'completed','failed'}: raise ValueError(f'unsupported function result status: {status}')
                        if line_offset>=offset:
                            ck,ok=_kind(name); component_id=_skill_id(record.get('output')) if ck=='skill' else None
                            events.append(ComponentEventV1(schema_version='component_event.v1',event_id=_event_id(installation_id,session_id,call,status),invocation_id=call,run_id=run_id or session_id,runtime='workbuddy',plugin_id='workbuddy-desktop-adapter',component_kind=ck,component_id=component_id or name,stage=status,occurred_at=occurred,started_at=starts[call][1],ended_at=occurred,error_code='tool_failed' if status=='failed' else None,observation_kind=ok,evidence_source='workbuddy_project_jsonl',evidence_level='verified',installation_id=installation_id,tenant_id=binding.tenant_id if binding else None,user_id=binding.user_id if binding else None,source_type='workbuddy_project_jsonl'))
                        continue
                    if isinstance(record.get('callId'),str): raise ValueError(f'unsupported correlated record type: {typ}')
                end=handle.tell()
        except (ValueError,UnicodeDecodeError,json.JSONDecodeError) as exc:
            raise WorkBuddyJsonlParseError(str(exc)) from exc
        next_cursor=CodexCursor(installation_id,str(source),identity,end,(now or datetime.now(timezone.utc)).astimezone(timezone.utc)); self.registry.save_cursor(next_cursor)
        return WorkBuddyJsonlReadResult(tuple(events),next_cursor)

__all__=['WorkBuddyJsonlAdapter','WorkBuddyJsonlParseError','WorkBuddyJsonlReadResult']
