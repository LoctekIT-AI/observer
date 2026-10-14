"""Versioned renderer read model projected from the Runtime event ledger."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import Field

from .base import ContractModel

CHAT_RUN_READ_MODEL_SCHEMA_VERSION = "cowork.runtime.chat-read-model.v1"


class ChatRunReadModel(ContractModel):
    schema_version: Literal["cowork.runtime.chat-read-model.v1"] = CHAT_RUN_READ_MODEL_SCHEMA_VERSION
    run_id: str
    session_id: str
    status: str
    terminal: bool = False
    cursor: int = Field(default=0, ge=0)
    timeline: List[Dict[str, Any]] = Field(default_factory=list)
    assistant_draft: Optional[str] = None
    assistant_final: Optional[str] = None
    artifacts: List[Dict[str, Any]] = Field(default_factory=list)
    approvals: List[Dict[str, Any]] = Field(default_factory=list)
    # Runtime-owned structured input; renderer must submit the opaque id and
    # option id rather than infer meaning from assistant prose.
    pending_input: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None
    last_activity_at: Optional[datetime] = None
