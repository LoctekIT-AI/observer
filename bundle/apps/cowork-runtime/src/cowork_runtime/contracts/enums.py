"""Shared runtime enum values.

These values are part of the public cross-process contract. Additive values are
allowed; renames must be handled through an explicit compatibility mapper.
"""

from __future__ import annotations

from enum import Enum


class RunStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_INPUT = "waiting_input"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


class TurnRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class TurnStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepKind(str, Enum):
    MODEL = "model"
    TOOL = "tool"
    APPROVAL = "approval"
    ARTIFACT = "artifact"
    SYSTEM = "system"


class StepStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ToolCallStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ApprovalStatus(str, Enum):
    REQUESTED = "requested"
    ALLOWED_ONCE = "allowed_once"
    ALLOWED_SESSION = "allowed_session"
    ALLOWED_PROJECT = "allowed_project"
    DENIED = "denied"
    EXPIRED = "expired"


class ArtifactAccessKind(str, Enum):
    LOCAL_FILE = "local_file"
    REMOTE_OBJECT = "remote_object"
    REMOTE_URL = "remote_url"
    INLINE = "inline"


class EventVisibility(str, Enum):
    USER = "user"
    DEBUG = "debug"
    AUDIT = "audit"
    INTERNAL = "internal"


class RuntimeEventType(str, Enum):
    RUN_CREATED = "run.created"
    RUN_STARTED = "run.started"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    RUN_BLOCKED = "run.blocked"
    TURN_CREATED = "turn.created"
    TEXT_DELTA = "text.delta"
    # Provider 已返回但尚未具备交付资格的中间文本。它是可回放的过程事实，
    # 绝不能被 Renderer 当作最终正文；只有 DeliverySettler/终态事件拥有正文权。
    TURN_ASSISTANT_DRAFT = "turn.assistant_draft"
    TURN_ASSISTANT_TEXT = "turn.assistant_text"
    USAGE_UPDATED = "usage.updated"
    TURN_COMPLETED = "turn.completed"
    STEP_CREATED = "step.created"
    STEP_COMPLETED = "step.completed"
    STEP_PROGRESS = "step_progress"
    AGENT_REASONING = "agent.reasoning"
    TOOL_CALL_CREATED = "tool_call.created"
    TOOL_RESULT_CREATED = "tool_result.created"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"
    CRAWLER_INTERACTION_REQUESTED = "crawler.interaction.requested"
    CRAWLER_INTERACTION_RESUMED = "crawler.interaction.resumed"
    ARTIFACT_CREATED = "artifact.created"
    FILE_CHANGED = "file.changed"
    STATE_SYNC = "state.sync"
    ERROR_RAISED = "error.raised"
    PIPELINE_STAGE = "pipeline.stage"
    RUN_HEARTBEAT = "run.heartbeat"
    TRACE_CONTEXT = "trace.context"
    DELEGATION_AGGREGATE = "delegation.aggregate"
    DELEGATION_VERIFICATION = "delegation.verification"
    COGNITIVE_GOAL_COMPILED = "cognitive.goal_compiled"
    COGNITIVE_PLAN_PROPOSED = "cognitive.plan_proposed"
    COGNITIVE_DECISION = "cognitive.decision"
    COGNITIVE_HYPOTHESIS = "cognitive.hypothesis"
    COGNITIVE_OBSERVATION = "cognitive.observation"
    COGNITIVE_BELIEF_UPDATE = "cognitive.belief_update"
    COGNITIVE_EFFECT_EXPECTED = "cognitive.effect_expected"
    COGNITIVE_EFFECT_OBSERVED = "cognitive.effect_observed"
    COGNITIVE_REPLAN = "cognitive.replan"
    COGNITIVE_VERDICT = "cognitive.verdict"


class RuntimeErrorCode(str, Enum):
    UNKNOWN = "unknown"
    INVALID_REQUEST = "invalid_request"
    BUDGET_EXCEEDED = "budget_exceeded"
    CANCELLED = "cancelled"
    MODEL_FAILED = "model_failed"
    TOOL_FAILED = "tool_failed"
    POLICY_DENIED = "policy_denied"
    APPROVAL_EXPIRED = "approval_expired"
    SEQUENCE_CONFLICT = "sequence_conflict"
    STORAGE_UNAVAILABLE = "storage_unavailable"
    UNKNOWN_OUTCOME = "unknown_outcome"
    CONTEXT_COMPACTION_FAILED = "context_compaction_failed"
