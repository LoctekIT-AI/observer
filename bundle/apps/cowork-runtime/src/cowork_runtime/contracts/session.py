"""Durable session lineage contracts used by local and remote Runtime."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import Field

from .base import ContractModel, utc_now


SessionMessageRole = Literal["system", "user", "assistant", "tool"]


class SessionMessage(ContractModel):
    message_id: str
    session_id: str
    run_id: str
    role: SessionMessageRole
    content: Any = None
    tool_call_id: Optional[str] = None
    tool_calls: List[Dict[str, Any]] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class PinnedConstraint(ContractModel):
    constraint_id: str
    session_id: str
    content: str
    source_run_id: Optional[str] = None
    active: bool = True
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class CompactionRecord(ContractModel):
    compaction_id: str
    session_id: str
    source_message_ids: List[str] = Field(default_factory=list)
    summary: str
    latest_message_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    # The lifecycle fields are additive so older SQLite documents remain
    # readable while pending summaries can be resumed after a restart.
    status: Literal["pending", "applied", "superseded", "failed"] = "pending"
    source_turn_ids: List[str] = Field(default_factory=list)
    source_digest: Optional[str] = None
    summary_document: Dict[str, Any] = Field(default_factory=dict)
    input_tokens: int = 0
    summary_tokens: int = 0
    applied_at: Optional[datetime] = None
    superseded_by: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
