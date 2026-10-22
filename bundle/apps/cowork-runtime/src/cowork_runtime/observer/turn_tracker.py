"""Group a client's own turn markers and the reported Skill / MCP calls into turns.

Each client parser yields markers in file order: ("start", at, {"turn_id"}),
("user", at, {"text"}), ("final", at, {"text"}), ("end", at, {"turn_id", "text", "aborted"}).
The worker adds its terminal Skill / MCP events; a turn that ends with at least one
of them becomes a telemetry.turns.Turn. The open turn of every source is kept in
<state>/turns-state.json, so a turn can span several polls.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from cowork_contracts import ComponentEventV1

from ..telemetry.turns import Turn

STALE_AFTER = timedelta(hours=6)    # an open turn with no activity for this long is closed as incomplete
FORGET_AFTER = timedelta(days=7)    # state of sources untouched this long is dropped
MAX_INPUTS = 20

Marker = tuple[str, datetime, dict[str, Any]]


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value)


class TurnTracker:
    def __init__(self, state_dir: Path, runtime: str) -> None:
        self.path = state_dir / "turns-state.json"
        self.runtime = runtime
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.state: dict[str, dict[str, Any]] = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self.state = {}

    def save(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        self.state = {key: value for key, value in self.state.items()
                      if now - _time(value.get("touched_at", _iso(now))) < FORGET_AFTER}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)

    # ------------------------------------------------------------------
    def feed(self, source: str, session_id: str, markers: Iterable[Marker], events: Iterable[ComponentEventV1],
             *, not_before: datetime | None = None, now: datetime | None = None, runtime: str | None = None) -> list[Turn]:
        now = now or datetime.now(timezone.utc)
        runtime = runtime or self.runtime
        items: list[tuple[datetime, int, str, dict[str, Any]]] = []
        for order, (kind, at, data) in enumerate(markers):
            items.append((at, 1 if kind == "end" else 0, kind, {**data, "_order": order}))
        for event in events:
            if event.stage == "started" or event.component_kind not in {"skill", "mcp_tool"}:
                continue
            items.append((event.occurred_at, 0, "call", {"kind": event.component_kind, "id": event.component_id, "_order": -1}))
        # File order for markers; a call at the same instant as an "end" belongs to that turn.
        items.sort(key=lambda item: (item[0], item[1]))
        entry = self.state.get(source) or {}
        current: dict[str, Any] | None = entry.get("open")
        turns: list[Turn] = []

        def close(status: str, at: datetime, final: str | None = None) -> None:
            nonlocal current
            if current is None:
                return
            if final is not None:
                current["final"] = final
            skills = list(dict.fromkeys(current.get("skills", [])))
            mcp = list(dict.fromkeys(current.get("mcp", [])))
            if (skills or mcp) and (not_before is None or at >= not_before):
                inputs = [text for text in current.get("inputs", []) if text]
                turns.append(Turn(
                    runtime=runtime, session_id=session_id, turn_id=current["turn_id"],
                    started_at=_time(current["started_at"]), ended_at=at,
                    user_input="\n\n".join(inputs) if inputs else None, final_output=current.get("final"),
                    skills=skills, mcp_tools=mcp, status=status))
            current = None

        def open_turn(at: datetime, turn_id: str | None) -> dict[str, Any]:
            implicit = hashlib.sha256(f"{source}:{_iso(at)}".encode()).hexdigest()[:24]
            return {"turn_id": turn_id or f"implicit-{implicit}", "started_at": _iso(at), "last_at": _iso(at),
                    "inputs": [], "final": None, "skills": [], "mcp": []}

        for at, _, kind, data in items:
            if current is not None and at - _time(current["last_at"]) > STALE_AFTER:
                close("incomplete", _time(current["last_at"]))
            if kind == "start":
                if current is not None and current.get("explicit"):
                    close("incomplete", at)
                if current is None:
                    current = open_turn(at, data.get("turn_id"))
                else:  # user input or calls seen just before the client's own start marker
                    current["turn_id"] = data.get("turn_id") or current["turn_id"]
                current["explicit"] = True
            elif kind == "user":
                if current is None:
                    current = open_turn(at, None)
                if len(current["inputs"]) < MAX_INPUTS and (not current["inputs"] or current["inputs"][-1] != data.get("text")):
                    current["inputs"].append(data.get("text"))
            elif kind == "final":
                if current is not None:
                    current["final"] = data.get("text")
            elif kind == "call":
                if current is None:
                    current = open_turn(at, None)
                current["skills" if data["kind"] == "skill" else "mcp"].append(data["id"])
            elif kind == "end":
                if current is not None:
                    if data.get("input") and not current["inputs"]:
                        current["inputs"] = [data["input"]]
                    close("aborted" if data.get("aborted") else "completed", at, data.get("text"))
            if current is not None:
                current["last_at"] = _iso(max(at, _time(current["last_at"])))
        if current is not None and now - _time(current["last_at"]) > STALE_AFTER:
            close("incomplete", _time(current["last_at"]))
        self.state[source] = {"open": current, "touched_at": _iso(now)}
        return turns


__all__ = ["Marker", "TurnTracker"]
