"""Versioned Feishu action contracts and capability catalog.

The catalog is intentionally data-first so the API and Desktop can validate
the same domain/operation surface without sharing business implementation or
credentials. CLI argument compilation remains in the Cowork domain layer.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .errors import ContractError, ErrorCode


FEISHU_CATALOG_VERSION = "cowork.feishu_capability_catalog.v1"
FEISHU_ACTION_VERSION = "feishu.action.v1"
FEISHU_CLIENT_ACTION_VERSION = "feishu.client_action.v1"
FEISHU_RESULT_VERSION = "feishu.result.v1"
FEISHU_DESKTOP_OPERATION = "feishu.execute"
_CATALOG_PATH = Path(__file__).with_name("feishu_capability_catalog.v1.json")
FEISHU_BOT_EVENT_VERSION = "cowork.feishu_bot_event.v1"


class FeishuBotUrlVerificationV1(BaseModel):
    """飞书事件订阅首次校验请求的最小契约。"""

    model_config = ConfigDict(extra="ignore")

    type: Literal["url_verification"]
    challenge: str = Field(min_length=1, max_length=4096)
    token: str = Field(min_length=1, max_length=4096)


class FeishuBotMessageEventV1(BaseModel):
    """只保留机器人续接需要的飞书消息字段，避免传播整份回调原文。"""

    model_config = ConfigDict(extra="ignore")

    version: Literal[FEISHU_BOT_EVENT_VERSION] = FEISHU_BOT_EVENT_VERSION
    event_id: str = Field(min_length=1, max_length=256)
    verification_token: str = Field(min_length=1, max_length=4096)
    message_id: str = Field(min_length=1, max_length=256)
    message_type: str = Field(min_length=1, max_length=32)
    message_text: str | None = Field(default=None, max_length=6000)
    root_id: str | None = Field(default=None, max_length=256)
    parent_id: str | None = Field(default=None, max_length=256)
    chat_id: str | None = Field(default=None, max_length=256)
    sender_open_id: str = Field(min_length=1, max_length=256)


def parse_feishu_bot_callback(payload: Mapping[str, Any]) -> FeishuBotUrlVerificationV1 | FeishuBotMessageEventV1:
    """解析飞书 v2 回调，且只输出 Cowork 需要的安全最小投影。"""

    if str(payload.get("type") or "") == "url_verification":
        return FeishuBotUrlVerificationV1.model_validate(payload)

    header = payload.get("header")
    event = payload.get("event")
    if not isinstance(header, Mapping) or not isinstance(event, Mapping):
        raise _contract_error(ErrorCode.VALIDATION_ERROR, "Feishu callback is missing event header")
    if str(header.get("event_type") or "") != "im.message.receive_v1":
        raise _contract_error(ErrorCode.VALIDATION_ERROR, "Feishu callback event type is unsupported")
    message = event.get("message")
    sender = event.get("sender")
    sender_id = sender.get("sender_id") if isinstance(sender, Mapping) else None
    if not isinstance(message, Mapping) or not isinstance(sender_id, Mapping):
        raise _contract_error(ErrorCode.VALIDATION_ERROR, "Feishu message callback is incomplete")

    message_type = str(message.get("message_type") or "").strip()
    message_text = None
    if message_type == "text":
        try:
            content = json.loads(str(message.get("content") or "{}"))
        except json.JSONDecodeError as exc:
            raise _contract_error(ErrorCode.VALIDATION_ERROR, "Feishu text message content is invalid") from exc
        if not isinstance(content, Mapping):
            raise _contract_error(ErrorCode.VALIDATION_ERROR, "Feishu text message content is invalid")
        message_text = str(content.get("text") or "").strip() or None

    return FeishuBotMessageEventV1(
        event_id=str(header.get("event_id") or "").strip(),
        verification_token=str(header.get("token") or "").strip(),
        message_id=str(message.get("message_id") or "").strip(),
        message_type=message_type,
        message_text=message_text,
        root_id=str(message.get("root_id") or "").strip() or None,
        parent_id=str(message.get("parent_id") or "").strip() or None,
        chat_id=str(message.get("chat_id") or "").strip() or None,
        sender_open_id=str(sender_id.get("open_id") or "").strip(),
    )


@lru_cache(maxsize=1)
def load_feishu_capability_catalog() -> dict[str, Any]:
    raw = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    if raw.get("version") != FEISHU_CATALOG_VERSION:
        raise RuntimeError(f"Unsupported Feishu capability catalog: {raw.get('version')}")
    if not isinstance(raw.get("capabilities"), list) or not raw["capabilities"]:
        raise RuntimeError("Feishu capability catalog must contain capabilities")
    return raw


def feishu_capabilities() -> tuple[Mapping[str, Any], ...]:
    return tuple(item for item in load_feishu_capability_catalog()["capabilities"] if isinstance(item, Mapping))


def feishu_capability(domain: str, operation: str) -> Mapping[str, Any] | None:
    normalized_domain = str(domain or "").strip().lower()
    normalized_operation = str(operation or "").strip().lower().replace("-", "_")
    return next(
        (
            item
            for item in feishu_capabilities()
            if item.get("domain") == normalized_domain and item.get("operation") == normalized_operation
        ),
        None,
    )


def feishu_domains() -> tuple[str, ...]:
    return tuple(sorted({str(item["domain"]) for item in feishu_capabilities()}))


def feishu_operations_by_domain() -> dict[str, frozenset[str]]:
    result: dict[str, set[str]] = {}
    for item in feishu_capabilities():
        result.setdefault(str(item["domain"]), set()).add(str(item["operation"]))
    return {domain: frozenset(operations) for domain, operations in result.items()}


def _contract_error(code: ErrorCode, message: str, **details: Any) -> ContractError:
    return ContractError(code, message, details=details)


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, Mapping)
    return False


def validate_feishu_action_params(domain: str, operation: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
    capability = feishu_capability(domain, operation)
    if capability is None:
        raise _contract_error(
            ErrorCode.FEISHU_ACTION_UNSUPPORTED,
            "Feishu domain or operation is not registered.",
            domain=domain,
            operation=operation,
        )
    schema = capability.get("params")
    if not isinstance(schema, Mapping):
        raise RuntimeError(f"Feishu capability has invalid params schema: {domain}.{operation}")
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        raise RuntimeError(f"Feishu capability has invalid properties: {domain}.{operation}")
    unknown = sorted(str(key) for key in params if key not in properties)
    if unknown:
        raise _contract_error(
            ErrorCode.FEISHU_ACTION_PARAMS_INVALID,
            "Feishu action contains unregistered parameters.",
            domain=domain,
            operation=operation,
            unknown_params=unknown,
        )
    required = [str(name) for name in schema.get("required", [])]
    missing = [name for name in required if name not in params or params[name] is None]
    if missing:
        raise _contract_error(
            ErrorCode.FEISHU_ACTION_PARAMS_INVALID,
            "Feishu action is missing required parameters: "
            + ", ".join(f"params.{name}" for name in missing),
            domain=domain,
            operation=operation,
            missing_params=missing,
        )
    for alternatives in schema.get("one_of_required", []):
        names = [str(name) for name in alternatives]
        if not any(name in params and params[name] not in (None, "") for name in names):
            raise _contract_error(
                ErrorCode.FEISHU_ACTION_PARAMS_INVALID,
                "Feishu action is missing one required parameter group: "
                + " or ".join(f"params.{name}" for name in names),
                domain=domain,
                operation=operation,
                one_of_required=names,
            )
    for name, value in params.items():
        rule = properties.get(name)
        expected = rule.get("type") if isinstance(rule, Mapping) else None
        expected_types = [expected] if isinstance(expected, str) else list(expected or [])
        if expected_types and not any(_matches_type(value, item) for item in expected_types):
            raise _contract_error(
                ErrorCode.FEISHU_ACTION_PARAMS_INVALID,
                "Feishu action parameter has an invalid type.",
                domain=domain,
                operation=operation,
                parameter=name,
                expected=expected_types,
            )
    return capability


def build_feishu_action_tool_schema(*, action_version: str = FEISHU_ACTION_VERSION) -> dict[str, Any]:
    """Build the model-facing schema from the same catalog as validation."""
    capabilities = list(feishu_capabilities())
    domains = sorted({str(item["domain"]) for item in capabilities if str(item["domain"]) != "auth"})
    action_variants: list[dict[str, Any]] = []
    for item in capabilities:
        if str(item["domain"]) == "auth":
            continue
        params = dict(item.get("params") or {})
        params["additionalProperties"] = False
        action_variants.append(
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "version": {"type": "string", "enum": [action_version]},
                    "domain": {"const": item["domain"]},
                    "operation": {"const": item["operation"]},
                    "params": params,
                    "output_format": {"type": "string", "enum": ["json"]},
                    "risk_hint": {"type": "string", "enum": ["read", "write", "delete", "admin"]},
                    "timeout_ms": {"type": "integer", "minimum": 1000, "maximum": 600000},
                },
                "required": ["domain", "operation"],
            }
        )
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "version": {"type": "string", "enum": [action_version]},
            "domain": {"type": "string", "enum": domains},
            "operation": {"type": "string"},
            "params": {"type": "object"},
            "output_format": {"type": "string", "enum": ["json"]},
            "risk_hint": {"type": "string", "enum": ["read", "write", "delete", "admin"]},
            "timeout_ms": {"type": "integer", "minimum": 1000, "maximum": 600000},
        },
        "required": ["domain", "operation"],
        "allOf": [{"oneOf": action_variants}],
    }


class FeishuActionV1(BaseModel):
    """Typed business action, independent from a local CLI command shape."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[FEISHU_ACTION_VERSION] = FEISHU_ACTION_VERSION
    domain: str = Field(min_length=1, max_length=40)
    operation: str = Field(min_length=1, max_length=80)
    params: dict[str, Any] = Field(default_factory=dict)
    timeout_ms: int | None = Field(default=None, ge=1_000, le=600_000)

    @field_validator("domain", "operation")
    @classmethod
    def normalize_key_part(cls, value: str) -> str:
        return value.strip().lower().replace("-", "_")

    @model_validator(mode="after")
    def validate_catalog_capability(self) -> "FeishuActionV1":
        validate_feishu_action_params(self.domain, self.operation, self.params)
        return self

    @property
    def action_key(self) -> str:
        return f"{self.domain}.{self.operation}"


class FeishuClientActionV1(BaseModel):
    """Desktop-bound typed action. ``cli_args`` are compiler output, never LLM input."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[FEISHU_CLIENT_ACTION_VERSION] = FEISHU_CLIENT_ACTION_VERSION
    client_action: Literal["feishu"] = "feishu"
    operation: Literal[FEISHU_DESKTOP_OPERATION] = FEISHU_DESKTOP_OPERATION
    action: FeishuActionV1
    cli_args: list[str] = Field(min_length=1)
    timeout_ms: int = Field(ge=1_000, le=600_000)

    @field_validator("cli_args")
    @classmethod
    def require_nonblank_cli_args(cls, value: list[str]) -> list[str]:
        if not all(isinstance(item, str) and item.strip() for item in value):
            raise ValueError("cli_args must contain nonblank strings")
        return value


class FeishuResultV1(BaseModel):
    """Stable result shell. ISSUE-049 will make it the frontend ReadModel input."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[FEISHU_RESULT_VERSION] = FEISHU_RESULT_VERSION
    action: FeishuActionV1
    status: Literal["success", "failed", "blocked", "cancelled"]
    result_kind: str = Field(min_length=1, max_length=80)
    data: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error: str | None = None


def parse_feishu_client_action(raw: Any) -> FeishuClientActionV1:
    if not isinstance(raw, Mapping):
        raise _contract_error(
            ErrorCode.FEISHU_CLIENT_ACTION_INVALID,
            "Feishu client action must be an object.",
        )
    if raw.get("version") != FEISHU_CLIENT_ACTION_VERSION:
        raise _contract_error(
            ErrorCode.FEISHU_CONTRACT_VERSION_UNSUPPORTED,
            "Unsupported Feishu client action version.",
            received_version=raw.get("version"),
            supported_version=FEISHU_CLIENT_ACTION_VERSION,
        )
    try:
        return FeishuClientActionV1.model_validate(raw)
    except (ValidationError, ContractError) as exc:
        if isinstance(exc, ContractError):
            raise exc
        raise _contract_error(
            ErrorCode.FEISHU_CLIENT_ACTION_INVALID,
            "Feishu client action does not match the registered contract.",
            validation_errors=exc.errors(include_url=False),
        ) from exc


def build_feishu_client_action(
    *,
    domain: str,
    operation: str,
    params: Mapping[str, Any],
    cli_args: list[str],
    timeout_ms: int,
) -> FeishuClientActionV1:
    return FeishuClientActionV1(
        action=FeishuActionV1(domain=domain, operation=operation, params=dict(params)),
        cli_args=cli_args,
        timeout_ms=timeout_ms,
    )
