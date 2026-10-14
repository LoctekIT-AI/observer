"""Versioned contracts for Cowork scheduled-job drafts.

These models describe a user-confirmed plan, not a worker implementation.
Menu creation and chat creation can both converge on the same fields before the
API writes a ``cowork_scheduled_jobs`` row.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SCHEDULE_DRAFT_VERSION = "cowork.schedule_draft.v1"
CREATE_SCHEDULED_JOB_INTENT_VERSION = "cowork.create_scheduled_job_intent.v1"
CANCEL_SCHEDULED_JOB_INTENT_VERSION = "cowork.cancel_scheduled_job_intent.v1"


class ScheduleSpecV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schedule_type: Literal["once", "cron"]
    cron_expr: str | None = Field(default=None, max_length=128)
    fire_at: datetime | None = None
    timezone: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def validate_schedule_shape(self) -> "ScheduleSpecV1":
        if self.schedule_type == "cron" and not self.cron_expr:
            raise ValueError("cron schedules require cron_expr")
        if self.schedule_type == "once" and self.fire_at is None:
            raise ValueError("once schedules require fire_at")
        if self.schedule_type == "cron" and self.fire_at is not None:
            raise ValueError("cron schedules must not carry fire_at")
        if self.schedule_type == "once" and self.cron_expr is not None:
            raise ValueError("once schedules must not carry cron_expr")
        return self


class NotificationTargetV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: Literal["feishu_bot_dm", "cowork_default"] = "feishu_bot_dm"
    recipient: Literal["current_user"] = "current_user"
    # OS introspect 提供的飞书 open_id 只作为 Cowork 定时任务投递地址投影，
    # 不是 Cowork 自建用户身份，也不能反查 OS 用户表。
    recipient_open_id: str | None = Field(default=None, min_length=1, max_length=256)
    fallback_channel: Literal["cowork_default"] = "cowork_default"


class ProjectPreferenceV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["scheduled_jobs_system_project", "explicit_project"] = "scheduled_jobs_system_project"
    project_id: str | None = None

    @model_validator(mode="after")
    def validate_project_preference(self) -> "ProjectPreferenceV1":
        if self.mode == "explicit_project" and not self.project_id:
            raise ValueError("explicit_project requires project_id")
        if self.mode == "scheduled_jobs_system_project" and self.project_id is not None:
            raise ValueError("system project preference must not carry project_id")
        return self


class ScheduleDraftV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[SCHEDULE_DRAFT_VERSION] = SCHEDULE_DRAFT_VERSION
    objective: str = Field(min_length=1, max_length=20_000)
    title: str = Field(min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=2_000)
    schedule: ScheduleSpecV1
    notification_target: NotificationTargetV1 = Field(default_factory=NotificationTargetV1)
    project_preference: ProjectPreferenceV1 = Field(default_factory=ProjectPreferenceV1)
    confirmation_digest: str = Field(min_length=64, max_length=64)
    parser: dict[str, Any] = Field(default_factory=dict)

    @field_validator("objective", "title")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("text fields must be nonblank")
        return normalized

    @model_validator(mode="after")
    def validate_digest(self) -> "ScheduleDraftV1":
        expected = schedule_draft_digest(self.model_dump(mode="json", exclude={"confirmation_digest", "version"}))
        if self.confirmation_digest != expected:
            raise ValueError("confirmation_digest does not match draft content")
        return self


class CreateScheduledJobIntentV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[CREATE_SCHEDULED_JOB_INTENT_VERSION] = CREATE_SCHEDULED_JOB_INTENT_VERSION
    draft: ScheduleDraftV1
    source: Literal["chat_confirmation", "menu"] = "chat_confirmation"
    idempotency_key: str = Field(min_length=16, max_length=160)


class CancelScheduledJobIntentV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[CANCEL_SCHEDULED_JOB_INTENT_VERSION] = CANCEL_SCHEDULED_JOB_INTENT_VERSION
    scheduled_job_id: str = Field(min_length=1, max_length=64)
    scheduled_job_title: str | None = Field(default=None, max_length=256)
    summary: str = Field(min_length=1, max_length=256)
    idempotency_key: str = Field(min_length=16, max_length=160)
    source_text: str | None = Field(default=None, max_length=512)


def schedule_draft_digest(payload: dict[str, Any]) -> str:
    stable = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def build_schedule_draft(**kwargs: Any) -> ScheduleDraftV1:
    payload = dict(kwargs)
    schedule = ScheduleSpecV1.model_validate(payload["schedule"]).model_dump(mode="json")
    notification_target = NotificationTargetV1.model_validate(payload.get("notification_target") or {}).model_dump(mode="json")
    project_preference = ProjectPreferenceV1.model_validate(payload.get("project_preference") or {}).model_dump(mode="json")
    normalized = {
        **payload,
        "schedule": schedule,
        "notification_target": notification_target,
        "project_preference": project_preference,
    }
    normalized["confirmation_digest"] = schedule_draft_digest(normalized)
    return ScheduleDraftV1.model_validate(normalized)
