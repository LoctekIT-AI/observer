"""Transport-independent RuntimeEvent emission boundary."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from cowork_runtime.contracts import CognitiveRecord, RuntimeEvent
from cowork_runtime.contracts.enums import EventVisibility, RuntimeEventType
from cowork_runtime.persistence.ports import EventStore

from .redaction import redact_sensitive

_EVENT_LOGGER = logging.getLogger("cowork.runtime.events")

# 需要输出内容摘要的事件类型：用于对照"用户实际看到了什么文本"。
_CONTENT_LENGTH_TYPES = {"text.delta", "turn.assistant_draft", "turn.assistant_text"}


def _summarize_payload(event_type: RuntimeEventType, payload: dict[str, Any]) -> str:
    """Compact one-line payload summary for the event flow log."""
    try:
        if event_type.value in _CONTENT_LENGTH_TYPES:
            content = payload.get("content")
            return f"content_chars={len(content) if isinstance(content, str) else 0}"
        if event_type.value == "step_progress":
            step_id = payload.get("step_id") or payload.get("tool_call_id") or "-"
            return (
                f"phase={payload.get('phase')} status={payload.get('status')} "
                f"title={payload.get('title')} step_id={step_id} "
                f"content={str(payload.get('content'))[:80]}"
            )
        if event_type.value == "pipeline.stage":
            return f"stage={payload.get('stage')} status={payload.get('status')}"
        if event_type.value == "agent.reasoning":
            return f"phase={payload.get('phase')} model_turn={payload.get('model_turn')}"
        if event_type.value in {"tool_call.created", "tool_result.created"}:
            return f"tool={payload.get('tool_name') or payload.get('tool') or '-'}"
        rendered = json.dumps(payload, ensure_ascii=False, default=str)
        return f"payload_chars={len(rendered)} preview={rendered[:120]}"
    except Exception:  # noqa: BLE001 - 摘要失败不能影响事件写入。
        return "summary_unavailable"


class TraceEmitter(Protocol):
    """Append one traceable RuntimeEvent through the configured fact store."""

    def emit(
        self,
        run_id: str,
        event_type: RuntimeEventType,
        payload: dict,
        *,
        visibility: EventVisibility = EventVisibility.USER,
        event_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        redact: bool = False,
    ) -> RuntimeEvent:
        ...

    def emit_cognitive(
        self,
        record: CognitiveRecord,
        *,
        visibility: EventVisibility = EventVisibility.DEBUG,
    ) -> RuntimeEvent:
        ...


@dataclass(frozen=True)
class EventStoreTraceEmitter:
    """Default emitter backed by the existing append-only EventStore."""

    event_store: EventStore

    def emit(
        self,
        run_id: str,
        event_type: RuntimeEventType,
        payload: dict,
        *,
        visibility: EventVisibility = EventVisibility.USER,
        event_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        redact: bool = False,
    ) -> RuntimeEvent:
        safe_payload = redact_sensitive(payload) if redact else payload
        event = self.event_store.next_event(
            run_id,
            event_type,
            safe_payload,
            visibility=visibility,
            event_id=event_id,
            trace_id=trace_id,
        )
        # 事件流水日志是排查"前端显示与后端事实不一致"的基准线：每个进入
        # SQLite ledger 的事件都在这里留下一行 seq+类型+摘要，可与 Desktop
        # 轮询日志和渲染层日志按 seq 对齐。
        _EVENT_LOGGER.info(
            "event.emitted run_id=%s seq=%s type=%s visibility=%s %s",
            run_id,
            event.seq,
            event_type.value,
            visibility.value,
            _summarize_payload(event_type, safe_payload),
        )
        return event

    def emit_cognitive(
        self,
        record: CognitiveRecord,
        *,
        visibility: EventVisibility = EventVisibility.DEBUG,
    ) -> RuntimeEvent:
        event_type = RuntimeEventType(f"cognitive.{record.record_type}")
        payload = record.model_dump(mode="json", exclude={"schema_version", "record_type", "run_id", "trace_id"})
        event = self.event_store.next_event(
            record.run_id,
            event_type,
            redact_sensitive(payload),
            visibility=visibility,
            trace_id=record.trace_id,
        )
        _EVENT_LOGGER.info(
            "event.emitted run_id=%s seq=%s type=%s visibility=%s record_type=%s",
            record.run_id,
            event.seq,
            event_type.value,
            visibility.value,
            record.record_type,
        )
        return event
