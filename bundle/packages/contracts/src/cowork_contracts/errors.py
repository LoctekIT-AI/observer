from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    TENANT_SCOPE_DENIED = "TENANT_SCOPE_DENIED"
    CONTRACT_VERSION_UNSUPPORTED = "CONTRACT_VERSION_UNSUPPORTED"
    PLATFORM_UNAVAILABLE = "PLATFORM_UNAVAILABLE"
    PLATFORM_TIMEOUT = "PLATFORM_TIMEOUT"
    CREDENTIAL_UNAVAILABLE = "CREDENTIAL_UNAVAILABLE"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    NOT_READY = "NOT_READY"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    FEISHU_CONTRACT_VERSION_UNSUPPORTED = "FEISHU_CONTRACT_VERSION_UNSUPPORTED"
    FEISHU_ACTION_UNSUPPORTED = "FEISHU_ACTION_UNSUPPORTED"
    FEISHU_ACTION_PARAMS_INVALID = "FEISHU_ACTION_PARAMS_INVALID"
    FEISHU_CLIENT_ACTION_INVALID = "FEISHU_CLIENT_ACTION_INVALID"
    FEISHU_DESKTOP_ACTION_UNSUPPORTED = "FEISHU_DESKTOP_ACTION_UNSUPPORTED"
    DESKTOP_CLIENT_ACTION_UNSUPPORTED = "DESKTOP_CLIENT_ACTION_UNSUPPORTED"


class ContractError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        request_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_id = request_id
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code.value,
                "message": self.message,
                "request_id": self.request_id,
                "details": self.details,
            }
        }
