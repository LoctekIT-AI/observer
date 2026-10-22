"""Conversation turns that used a Skill or MCP: the user's input and the final reply.

One record per turn, sent next to the call records of the same conversation (same
Langfuse trace and session), so a turn can be evaluated together with the Skills
and MCP tools it used. Turns without any Skill / MCP call are never recorded.

Records wait in a small spool folder (one JSON file each) until Langfuse accepts
them, so a network outage does not lose them; the record id is derived from the
client's own turn id, so sending one twice overwrites instead of duplicating.
Set OBSERVER_CAPTURE_TURNS=0 (environment or customer.env) to switch this off.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

MAX_TEXT = 20_000          # characters kept of each text; longer text is cut and marked
MAX_SPOOL = 2_000          # turns kept waiting at most (oldest dropped first)


def capture_enabled() -> bool:
    return os.environ.get("OBSERVER_CAPTURE_TURNS", "1").strip().lower() not in {"0", "false", "off", "no"}


@dataclass
class Turn:
    runtime: str
    session_id: str
    turn_id: str
    started_at: datetime
    ended_at: datetime
    user_input: str | None
    final_output: str | None
    skills: list[str] = field(default_factory=list)
    mcp_tools: list[str] = field(default_factory=list)
    status: str = "completed"   # completed | aborted | incomplete

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["started_at"] = self.started_at.astimezone(timezone.utc).isoformat()
        data["ended_at"] = self.ended_at.astimezone(timezone.utc).isoformat()
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Turn":
        values = dict(data)
        values["started_at"] = datetime.fromisoformat(values["started_at"])
        values["ended_at"] = datetime.fromisoformat(values["ended_at"])
        return cls(**values)


def _uuid_hex(value: str) -> str:
    # Same derivation as telemetry.langfuse._uuid_hex: the turn lands in the calls' trace.
    return uuid.uuid5(uuid.NAMESPACE_DNS, f"aihub-skill-mcp:{value}").hex


def _nanos(value: datetime) -> int:
    value = value.astimezone(timezone.utc)
    return int(value.timestamp()) * 1_000_000_000 + value.microsecond * 1_000


def _cut(text: str | None) -> tuple[str | None, bool]:
    if text is None:
        return None, False
    return (text, False) if len(text) <= MAX_TEXT else (text[:MAX_TEXT], True)


def turn_payload(turn: Turn) -> tuple[dict[str, Any], str, str]:
    trace_id = _uuid_hex(turn.session_id)
    span_id = _uuid_hex(f"turn:{turn.runtime}:{turn.session_id}:{turn.turn_id}")[:16]
    user_input, input_cut = _cut(turn.user_input)
    final_output, output_cut = _cut(turn.final_output)
    started, ended = _nanos(turn.started_at), _nanos(turn.ended_at)
    values: list[tuple[str, object]] = [
        ("langfuse.observation.type", "span"),
        ("langfuse.session.id", turn.session_id),
        ("langfuse.observation.input", user_input),
        ("langfuse.observation.output", final_output),
        ("cowork.runtime", turn.runtime),
        ("cowork.component_kind", "turn"),
        ("cowork.component_id", "observer.turn"),
        ("cowork.turn.id", turn.turn_id),
        ("cowork.turn.status", turn.status),
        ("cowork.turn.skills", ",".join(turn.skills) or None),
        ("cowork.turn.mcp_tools", ",".join(turn.mcp_tools) or None),
        ("cowork.turn.input_truncated", input_cut),
        ("cowork.turn.output_truncated", output_cut),
    ]
    attributes = []
    for key, value in values:
        if value is None:
            continue
        attributes.append({"key": key, "value": {"boolValue": value} if isinstance(value, bool) else {"stringValue": str(value)}})
    span = {"traceId": trace_id, "spanId": span_id, "name": "observer.turn", "kind": 1,
            "startTimeUnixNano": min(started, ended), "endTimeUnixNano": ended,
            "attributes": attributes, "status": {"code": 2 if turn.status == "aborted" else 1}}
    body = {"resourceSpans": [{
        "resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "aihub-skill-mcp"}},
            {"key": "service.version", "value": {"stringValue": "observer.turn.v1"}},
        ]},
        "scopeSpans": [{"scope": {"name": "observer-turns"}, "spans": [span]}],
    }]}
    return body, trace_id, span_id


class TurnSpool:
    """Turns waiting to be sent, one JSON file each in <state>/turns/."""

    def __init__(self, state_dir: Path) -> None:
        self.directory = state_dir / "turns"

    def add(self, turn: Turn) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        _, _, span_id = turn_payload(turn)
        path = self.directory / f"{span_id}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(turn.to_json(), ensure_ascii=False), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        waiting = sorted(self.directory.glob("*.json"), key=lambda item: item.stat().st_mtime)
        for old in waiting[:-MAX_SPOOL]:
            old.unlink(missing_ok=True)

    def pending(self) -> int:
        return len(list(self.directory.glob("*.json"))) if self.directory.is_dir() else 0

    def flush(self, send: Callable[[dict[str, Any], str, str], object]) -> tuple[int, int]:
        """Send waiting turns; returns (sent, failed). A failed one stays for the next poll."""
        sent = failed = 0
        if not self.directory.is_dir():
            return 0, 0
        for path in sorted(self.directory.glob("*.json")):
            try:
                turn = Turn.from_json(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError, KeyError):
                path.unlink(missing_ok=True)  # unreadable: nothing to retry
                continue
            body, trace_id, span_id = turn_payload(turn)
            try:
                send(body, trace_id, span_id)
            except Exception as exc:  # retried next poll; never breaks collection
                status = getattr(exc, "status_code", None)
                if isinstance(status, int) and 400 <= status < 500 and status not in {401, 403, 408, 429}:
                    path.unlink(missing_ok=True)  # refused for good (e.g. too large): do not block the rest
                failed += 1
                break
            path.unlink(missing_ok=True)
            sent += 1
        return sent, failed


def default_send(body: dict[str, Any], trace_id: str, span_id: str) -> object:
    from .langfuse import LangfuseConfig, LangfuseExporter

    return LangfuseExporter(LangfuseConfig.from_env()).export_body(body, trace_id=trace_id, span_id=span_id)


__all__ = ["MAX_TEXT", "Turn", "TurnSpool", "capture_enabled", "default_send", "turn_payload"]
