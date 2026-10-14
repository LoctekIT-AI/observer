"""Strict reader for metadata projected by the native DSH Cordis plugin."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from pydantic import ValidationError
from cowork_contracts import ComponentEventV1

PLUGIN_ID = "dsh-observer"
SOURCE_TYPE = "dsh_native_metadata_jsonl"
_METADATA_FIELDS = frozenset({
    "schema_version", "event_id", "invocation_id", "run_id", "runtime", "plugin_id", "installation_id",
    "occurred_at", "started_at", "ended_at", "parent_invocation_id", "evidence_level", "source_type",
    "component_kind", "component_id", "error_code", "stage", "observation_kind", "evidence_source",
})


class DshAdapter:
    """Validate the native allowlist; never ingest transcript or tool bodies."""

    @staticmethod
    def validate(record: object) -> ComponentEventV1:
        if not isinstance(record, dict) or set(record) - _METADATA_FIELDS:
            raise ValueError("DSH source must contain metadata-only component events")
        try:
            event = ComponentEventV1.model_validate(record)
        except ValidationError as exc:
            # 不在异常文本中复制坏记录：源文件可能含未经允许的私密内容。
            raise ValueError("invalid DSH metadata fields") from exc
        if event.runtime != "dsh" or event.plugin_id != PLUGIN_ID or event.source_type != SOURCE_TYPE:
            raise ValueError("invalid DSH native source identity")
        expected_sources = {
            "skill_user_invocation": "dsh:session/event:skill-invocation",
            "skill_tool_result": "dsh:tools/result",
            "tool_result": "dsh:tools/result",
        }
        if event.evidence_source != expected_sources.get(event.observation_kind) or event.evidence_level != "verified":
            raise ValueError("invalid DSH native evidence")
        if event.stage == "started" or event.started_at is not None or event.ended_at != event.occurred_at:
            raise ValueError("DSH source requires native terminal metadata with no invented start time")
        if event.observation_kind.startswith("skill_") and event.component_kind != "skill":
            raise ValueError("DSH skill evidence requires a Skill identity")
        return event

    def read(self, path: Path, *, offset: int = 0) -> Iterator[tuple[ComponentEventV1, int]]:
        with path.open("rb") as handle:
            handle.seek(offset)
            while raw := handle.readline():
                position = handle.tell() - len(raw)
                if not raw.endswith(b"\n"):
                    raise ValueError(f"truncated DSH JSONL record at offset {position}: {path}")
                try:
                    record = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError(f"invalid DSH JSONL record at offset {position}: {path}") from exc
                yield self.validate(record), handle.tell()
