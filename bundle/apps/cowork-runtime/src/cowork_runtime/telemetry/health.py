"""Health records: each collector regularly tells the server how it is doing.

Without them a collector that silently stopped reading (a client update changed
its storage, a login expired, uploads keep failing) looks exactly like an
employee who simply did not use any Skill. A health record is one small OTLP
span named ``observer.health`` sent through the normal upload path (the server
stamps the employee as for every other record). It carries only versions,
states, counters and error *codes* -- no paths, no content.

A record is sent when the state changes (for example ok -> failed) and at least
once per ``interval_seconds`` (default 1 hour) otherwise.
"""
from __future__ import annotations

import json
import os
import platform
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

BUNDLE_ROOT = Path(__file__).resolve().parents[5]
DEFAULT_INTERVAL_SECONDS = 3600
_COUNTERS = ("events", "terminal_enqueued", "files", "sessions", "skipped_records", "unrecognized_calls", "replayed_sources")


def bundle_version() -> str:
    try:
        return str(json.loads((BUNDLE_ROOT / "manifest.json").read_text(encoding="utf-8")).get("bundle_version") or "unknown")
    except (OSError, ValueError):
        return "unknown"


def updater_status() -> dict[str, Any]:
    """Last result of the self-updater (scripts/observer_update.py), if installed."""
    try:
        value = json.loads((BUNDLE_ROOT.parent / "updater" / "status.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _attribute(key: str, value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": value}}
    return {"key": key, "value": {"stringValue": str(value)[:256]}}


def _nanos(value: datetime) -> int:
    return int(value.timestamp()) * 1_000_000_000 + value.microsecond * 1_000


def health_payload(runtime: str, status: dict[str, Any], *, now: datetime, totals: dict[str, int]) -> tuple[dict[str, Any], str, str]:
    """OTLP/JSON body for one health record (metadata only)."""
    installation = os.environ.get("OBSERVER_INSTALLATION_ID", "") or platform.node()
    trace_id = uuid.uuid5(uuid.NAMESPACE_DNS, f"aihub-skill-mcp:observer-health:{installation}:{runtime}").hex
    span_id = uuid.uuid4().hex[:16]
    errors = status.get("errors") or []
    codes = sorted({f"{item.get('code', 'error')}:{item.get('error_type', '')}".rstrip(":") for item in errors if isinstance(item, dict)})
    if status.get("error_type") and not codes:
        codes = [f"error:{status['error_type']}"]
    outbox = status.get("outbox") if isinstance(status.get("outbox"), dict) else {}
    values: list[tuple[str, object]] = [
        ("observer.health.runtime", runtime),
        ("observer.health.status", status.get("status", "unknown")),
        ("observer.health.bundle_version", bundle_version()),
        ("observer.health.os", platform.system() or sys.platform),
        ("observer.health.os_release", platform.release()),
        ("observer.health.python", platform.python_version()),
        ("observer.health.error_codes", ",".join(codes) or None),
        ("observer.health.outbox_pending", int(outbox.get("pending", 0))),
        ("observer.health.outbox_delivered", int(outbox.get("delivered", 0))),
        ("observer.health.outbox_failed", int(outbox.get("failed", 0))),
    ]
    update = updater_status()
    values += [
        ("observer.health.update_status", update.get("status")),
        ("observer.health.update_available_version", update.get("available_version")),
        ("observer.health.update_checked_at", update.get("checked_at")),
        ("observer.health.update_error", update.get("last_error")),
    ]
    values += [(f"observer.health.total_{name}", int(totals.get(name, 0))) for name in _COUNTERS]
    attributes = [item for key, value in values if (item := _attribute(key, value)) is not None]
    attributes += [
        {"key": "langfuse.trace.name", "value": {"stringValue": "observer.health"}},
        {"key": "langfuse.observation.type", "value": {"stringValue": "event"}},
        {"key": "langfuse.session.id", "value": {"stringValue": f"observer-health:{runtime}"}},
        {"key": "cowork.runtime", "value": {"stringValue": runtime}},
        {"key": "cowork.component_kind", "value": {"stringValue": "observer_health"}},
    ]
    stamp = _nanos(now)
    span = {
        "traceId": trace_id, "spanId": span_id, "name": "observer.health", "kind": 1,
        "startTimeUnixNano": stamp, "endTimeUnixNano": stamp, "attributes": attributes,
        "status": {"code": 1 if status.get("status") == "ok" else 2},
    }
    body = {"resourceSpans": [{
        "resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "aihub-skill-mcp"}},
            {"key": "service.version", "value": {"stringValue": "observer.health.v1"}},
        ]},
        "scopeSpans": [{"scope": {"name": "observer-health"}, "spans": [span]}],
    }]}
    return body, trace_id, span_id


class HealthReporter:
    """Decide when to send a health record and keep running totals in the state folder."""

    def __init__(
        self,
        state_dir: Path,
        runtime: str,
        *,
        enabled: bool = True,
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
        send: Callable[[dict[str, Any], str, str], object] | None = None,
    ) -> None:
        self.path = Path(state_dir) / "health.json"
        self.runtime = runtime
        self.enabled = enabled
        self.interval_seconds = interval_seconds
        self._send = send

    def _load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, state: dict[str, Any]) -> None:
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.path)

    def observe(self, status: dict[str, Any], *, now: datetime | None = None, **counts: int) -> bool:
        """Record one poll; send a health record when due. Never raises."""
        try:
            current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
            state = self._load()
            totals = {name: int(state.get("totals", {}).get(name, 0)) for name in _COUNTERS}
            for name in _COUNTERS:
                value = counts.get(name, status.get(name) if name in {"events", "terminal_enqueued"} else 0)
                if isinstance(value, int) and value > 0 and name not in {"files", "sessions", "skipped_records"}:
                    totals[name] += value
            for name in ("files", "sessions", "skipped_records"):
                value = counts.get(name, status.get(name))
                if isinstance(value, int):
                    totals[name] = value  # current values, not sums
            state["totals"] = totals
            last_sent = state.get("last_sent_at")
            version = bundle_version()
            due = (
                last_sent is None
                or state.get("last_status") != status.get("status")
                or state.get("last_version") != version
                or (current - datetime.fromisoformat(last_sent)).total_seconds() >= self.interval_seconds
            )
            sent = False
            if due and self.enabled:
                body, trace_id, span_id = health_payload(self.runtime, status, now=current, totals=totals)
                try:
                    (self._send or _default_send)(body, trace_id, span_id)
                    state["last_sent_at"] = current.isoformat()
                    state["last_status"] = status.get("status")
                    state["last_version"] = version
                    state.pop("last_send_error", None)
                    sent = True
                except Exception as exc:  # health must never break collection
                    state["last_send_error"] = type(exc).__name__
            self._save(state)
            return sent
        except Exception:
            return False


def _default_send(body: dict[str, Any], trace_id: str, span_id: str) -> object:
    from .langfuse import LangfuseConfig, LangfuseExporter

    return LangfuseExporter(LangfuseConfig.from_env()).export_body(body, trace_id=trace_id, span_id=span_id)


__all__ = ["HealthReporter", "bundle_version", "health_payload"]
