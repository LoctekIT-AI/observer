"""Turns with a Skill / MCP call: user input + final reply, one record per turn (Codex first)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from cowork_runtime.observer.codex_worker import CodexObserverWorker
from cowork_runtime.telemetry.turns import MAX_TEXT, Turn, TurnSpool, turn_payload

SKILL = "/Users/u/.agents/skills/test-driven-development/SKILL.md"


def _ts(at):
    return at.isoformat().replace("+00:00", "Z")


def _ms(at):
    return int(at.timestamp() * 1000)


def _event(at, payload):
    return {"timestamp": _ts(at), "type": "event_msg", "payload": payload}


def _turn(at, turn_id, *, user="帮我写测试", final="写好了", skill=True, complete=True):
    records = [
        _event(at, {"type": "task_started", "turn_id": turn_id}),
        _event(at, {"type": "item_completed", "item": {"type": "UserMessage", "id": "u", "content": [{"type": "text", "text": user}]}}),
        _event(at, {"type": "item_completed", "item": {"type": "AgentMessage", "id": "a1", "phase": "commentary", "content": [{"type": "Text", "text": "我先看看"}]}}),
    ]
    if skill:
        records.append(_event(at + timedelta(seconds=1), {"type": "item_completed", "started_at_ms": _ms(at), "completed_at_ms": _ms(at),
            "item": {"type": "CommandExecution", "id": f"x_{turn_id}", "status": "completed", "command": ["/bin/zsh", "-lc", f"cat {SKILL}"], "aggregated_output": "body"}}))
    if complete:
        records += [
            _event(at + timedelta(seconds=2), {"type": "item_completed", "item": {"type": "AgentMessage", "id": "a2", "phase": "final_answer", "content": [{"type": "Text", "text": final}]}}),
            _event(at + timedelta(seconds=2), {"type": "task_complete", "turn_id": turn_id, "last_agent_message": final}),
        ]
    return records


def _write(path, records):
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _spooled(state):
    return sorted((json.loads(p.read_text()) for p in (state / "turns").glob("*.json")), key=lambda t: t["started_at"]) if (state / "turns").is_dir() else []


def _worker(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir(exist_ok=True)
    worker = CodexObserverWorker(source_root=root, state_dir=tmp_path / "state", flush=False)
    worker.run_once()
    return worker, root / "rollout-x.jsonl"


def test_turn_with_skill_is_recorded_with_input_and_final_reply(tmp_path):
    worker, rollout = _worker(tmp_path)
    now = datetime.now(timezone.utc)
    _write(rollout, [{"timestamp": _ts(now), "type": "session_meta", "payload": {"id": "sess-1"}}])
    _write(rollout, _turn(now, "t1"))
    _write(rollout, _turn(now + timedelta(minutes=1), "t2", user="随便聊聊", final="好的", skill=False))
    assert not worker.run_once().errors
    turns = _spooled(tmp_path / "state")
    assert len(turns) == 1
    turn = turns[0]
    assert (turn["turn_id"], turn["user_input"], turn["final_output"], turn["skills"], turn["status"]) == (
        "t1", "帮我写测试", "写好了", ["test-driven-development"], "completed")
    worker.close()


def test_turn_spanning_two_polls(tmp_path):
    worker, rollout = _worker(tmp_path)
    now = datetime.now(timezone.utc)
    _write(rollout, [{"timestamp": _ts(now), "type": "session_meta", "payload": {"id": "sess-1"}}])
    _write(rollout, _turn(now, "t1", complete=False))
    worker.run_once()
    assert _spooled(tmp_path / "state") == []  # still running: nothing yet
    _write(rollout, [_event(now + timedelta(seconds=5), {"type": "task_complete", "turn_id": "t1", "last_agent_message": "完成"})])
    worker.close()
    worker = CodexObserverWorker(source_root=rollout.parent, state_dir=tmp_path / "state", flush=False)  # restart in between
    worker.run_once()
    turns = _spooled(tmp_path / "state")
    assert [(t["user_input"], t["final_output"]) for t in turns] == [("帮我写测试", "完成")]
    worker.close()


def test_spool_sends_and_keeps_failures(tmp_path):
    spool = TurnSpool(tmp_path)
    now = datetime.now(timezone.utc)
    spool.add(Turn("codex", "s", "t", now, now, "问" * (MAX_TEXT + 5), "答", ["a"], []))
    calls = []
    def broken(body, trace_id, span_id):
        raise RuntimeError("offline")
    assert spool.flush(broken) == (0, 1) and spool.pending() == 1
    assert spool.flush(lambda *args: calls.append(args)) == (1, 0) and spool.pending() == 0
    body, trace_id, span_id = calls[0]
    attributes = {a["key"]: a["value"] for a in body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]}
    assert len(attributes["langfuse.observation.input"]["stringValue"]) == MAX_TEXT
    assert attributes["cowork.turn.input_truncated"] == {"boolValue": True}
    assert attributes["cowork.component_kind"] == {"stringValue": "turn"}
    assert attributes["cowork.turn.skills"] == {"stringValue": "a"}
    from cowork_runtime.telemetry.langfuse import _uuid_hex
    assert trace_id == _uuid_hex("s")  # same trace as the calls of that conversation


def test_capture_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv("OBSERVER_CAPTURE_TURNS", "0")
    worker, rollout = _worker(tmp_path)
    now = datetime.now(timezone.utc)
    _write(rollout, [{"timestamp": _ts(now), "type": "session_meta", "payload": {"id": "sess-1"}}])
    _write(rollout, _turn(now, "t1"))
    worker.run_once()
    assert _spooled(tmp_path / "state") == []
    worker.close()


def test_workbuddy_ai_turn_text_comes_from_its_own_conversation_file(tmp_path, monkeypatch):
    import subprocess
    import sys
    from pathlib import Path
    from cowork_runtime.observer.workbuddy_worker import WorkBuddyDesktopObserverWorker

    bridge = Path(__file__).resolve().parents[1] / "bundle" / "scripts" / "workbuddy_hook_bridge.py"
    events = tmp_path / "events.jsonl"
    events.touch()

    def hook(stage, payload):
        result = subprocess.run([sys.executable, str(bridge), "--stage", stage, "--event-file", str(events), "--client", "workbuddy-ai"],
                                input=json.dumps(payload), text=True, capture_output=True)
        assert result.returncode == 0, result.stderr

    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    worker = WorkBuddyDesktopObserverWorker(source_root=events, state_dir=tmp_path / "state", flush=False)
    worker.run_once()
    session = "52f2c806-e255-45f5-a6e0-241219f2dc1d"
    hook("user_input", {"session_id": session, "hook_event_name": "UserPromptSubmit", "prompt": "x"})
    call = {"session_id": session, "tool_name": "Skill", "tool_use_id": "c1", "tool_input": {"skill": "workbuddy-finance-skill"}}
    hook("started", {**call, "hook_event_name": "PreToolUse"})
    hook("completed", {**call, "hook_event_name": "PostToolUse", "tool_response": "/Users/u/.workbuddy-ai/skills/workbuddy-finance-skill/SKILL.md"})
    now = datetime.now(timezone.utc)
    conversation = home / ".workbuddy-ai" / "projects" / "Users-u-WorkBuddy AI-2026-10-22" / f"{session}.jsonl"
    conversation.parent.mkdir(parents=True)
    ms = int(now.timestamp() * 1000)
    conversation.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in [
        {"type": "message", "role": "user", "timestamp": ms - 5000,
         "content": [{"type": "input_text", "text": "<system-reminder>ctx</system-reminder><user_query>查一下腾讯股价</user_query>"}]},
        {"type": "function_call", "name": "Skill", "timestamp": ms - 4000},
        {"type": "message", "role": "assistant", "timestamp": ms - 3000, "content": [{"type": "output_text", "text": "我先查一下"}]},
        {"type": "message", "role": "assistant", "timestamp": ms - 1000, "content": [{"type": "output_text", "text": "腾讯控股现价 500 港元"}]},
    ]) + "\n", encoding="utf-8")
    hook("final_output", {"session_id": session, "hook_event_name": "Stop"})
    assert not worker.run_once().errors
    turns = _spooled(tmp_path / "state")
    assert [(t["runtime"], t["user_input"], t["final_output"], t["skills"]) for t in turns] == [
        ("workbuddy-ai", "查一下腾讯股价", "腾讯控股现价 500 港元", ["workbuddy-finance-skill"])]
    worker.close()


def test_dsh_plugin_writes_turns_and_worker_queues_them(tmp_path):
    import shutil
    import subprocess
    from pathlib import Path
    import pytest
    from cowork_runtime.observer.dsh_worker import DshObserverWorker

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    plugin = Path(__file__).resolve().parents[1] / "bundle" / "packages" / "dsh-observer" / "index.mjs"
    events = tmp_path / "dsh" / "component-events.jsonl"
    script = f"""
import {{ apply }} from {json.dumps(plugin.as_uri())};
const handlers = {{}};
const ctx = {{ on: (name, fn) => (handlers[name] ||= []).push(fn), logger: {{ info: m => console.error(m) }} }};
apply(ctx, {{ eventFile: {json.dumps(str(events))}, installationId: 'install-dsh-1' }});
const session = {{ id: 'sess-9' }};
const fire = (name, ...args) => handlers[name].forEach(fn => fn(...args));
const ev = (type, data, time) => fire('session/event', session, {{ type, seq: 1, time, data }});
const t0 = Date.parse('2026-10-22T08:00:00Z');
ev('turn/start', {{ turn: 3 }}, t0);
ev('user/message', {{ source: {{ kind: 'user' }}, content: [{{ type: 'text', text: '帮我做海报' }}] }}, t0 + 1);
ev('assistant/message', {{ message: {{ content: [{{ type: 'text', text: '我先加载技能' }}] }} }}, t0 + 2);
fire('tools/execute', {{ agent: {{ session }}, token: 'k', callId: 'c1' }}, () => undefined);
fire('tools/result', {{ agent: {{ session }}, token: 'k', callId: 'c1', name: 'skill', arguments: {{}} }}, {{ isError: false, value: {{ name: 'poster-design-studio' }} }});
ev('assistant/message', {{ message: {{ content: [{{ type: 'text', text: '海报做好了' }}] }} }}, t0 + 3);
ev('turn/end', {{ turn: 3, reason: {{ kind: 'completed' }} }}, t0 + 4);
ev('turn/start', {{ turn: 4 }}, t0 + 5);
ev('user/message', {{ source: {{ kind: 'user' }}, content: [{{ type: 'text', text: '谢谢' }}] }}, t0 + 6);
ev('turn/end', {{ turn: 4, reason: {{ kind: 'completed' }} }}, t0 + 7);
"""
    runner = tmp_path / "run.mjs"
    runner.write_text(script, encoding="utf-8")
    result = subprocess.run([node, str(runner)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    lines = [json.loads(line) for line in (events.parent / "turns.ndjson").read_text().splitlines()]
    assert len(lines) == 1  # the turn without a Skill / MCP is not written
    assert (lines[0]["user_input"], lines[0]["final_output"], lines[0]["skills"], lines[0]["status"]) == (
        "帮我做海报", "海报做好了", ["poster-design-studio"], "completed")
    worker = DshObserverWorker(source_root=events.parent, state_dir=tmp_path / "state", flush=False)
    worker.run_once()
    worker.run_once()  # read once only
    turns = _spooled(tmp_path / "state")
    assert [(t["runtime"], t["session_id"], t["user_input"], t["skills"]) for t in turns] == [
        ("dsh", "sess-9", "帮我做海报", ["poster-design-studio"])]
    worker.close()


def _v8(value) -> bytes:
    """Tiny V8 serializer for the test (objects, sparse arrays, strings, ints, booleans)."""
    def varint(n):
        out = bytearray()
        while True:
            byte = n & 0x7F
            n >>= 7
            out.append(byte | (0x80 if n else 0))
            if not n:
                return bytes(out)
    if isinstance(value, bool):
        return b"T" if value else b"F"
    if isinstance(value, int):
        return b"I" + varint((value << 1) ^ (value >> 63))
    if value is None:
        return b"_"
    if isinstance(value, str):
        if all(ord(c) < 256 for c in value):
            raw = value.encode("latin-1")
            return b'"' + varint(len(raw)) + raw
        raw = value.encode("utf-16-le")
        return b"c" + varint(len(raw)) + raw
    if isinstance(value, list):
        body = b"".join(_v8(i) + _v8(item) for i, item in enumerate(value))
        return b"a" + varint(len(value)) + body + b"@" + varint(len(value)) + varint(len(value))
    if isinstance(value, dict):
        body = b"".join(_v8(k) + _v8(v) for k, v in value.items())
        return b"o" + body + b"{" + varint(len(value))
    raise TypeError(value)


def test_claude_desktop_turns_from_the_conversation_cache(tmp_path):
    from cowork_runtime.observer.claude_worker import ClaudeDesktopObserverWorker as Worker

    root = tmp_path / "https_claude.ai_0.indexeddb.leveldb"
    root.mkdir()
    (root / "000001.log").write_bytes(b"\x00" * 16)
    worker = Worker(source_root=root, state_dir=tmp_path / "state", flush=False)
    worker.run_once()  # installation; turns start being recorded from now
    later = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    tree = {"kind": "hub_transcript", "conversationUuid": "conv-1", "messages": [
        {"uuid": "h1", "sender": "human", "index": 0, "created_at": later,
         "content": [{"type": "text", "text": "请调用 observer_workspace_check 检查目录"}]},
        {"uuid": "a1", "sender": "assistant", "index": 1, "created_at": later, "updated_at": later, "stop_reason": "end_turn",
         "content": [{"type": "text", "text": "我来检查"},
                     {"type": "tool_use", "id": "t1", "name": "mcp__observer-workspace__observer_workspace_check", "input": {"summary": "x"}},
                     {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "{}"}]},
                     {"type": "text", "text": "检查通过：目录里有 AGENTS.md"}]},
        {"uuid": "h2", "sender": "human", "index": 2, "created_at": later, "content": [{"type": "text", "text": "谢谢"}]},
        {"uuid": "a2", "sender": "assistant", "index": 3, "created_at": later, "stop_reason": "end_turn",
         "content": [{"type": "text", "text": "不客气"}]},
    ]}
    value = _v8({"conversationUuid": "conv-1", "tree": tree})
    blob = tmp_path / "https_claude.ai_0.indexeddb.blob" / "1" / "01"
    blob.mkdir(parents=True)
    (blob / "7").write_bytes(b"\xff\x10" + value)  # a large value kept outside LevelDB
    worker._turn_scanned_at = None  # skip the once-a-minute wait
    worker.run_once()
    worker._turn_scanned_at = None
    worker.run_once()  # each turn once
    turns = _spooled(tmp_path / "state")
    assert [(t["runtime"], t["session_id"], t["user_input"], t["final_output"], t["mcp_tools"]) for t in turns] == [
        ("claude", "conv-1", "请调用 observer_workspace_check 检查目录", "检查通过：目录里有 AGENTS.md",
         ["mcp__observer-workspace__observer_workspace_check"])]
    worker.close()
