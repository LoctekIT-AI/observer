"""Small, explicit OTLP/HTTP exporter for the internal Langfuse observer.

Langfuse is an optional observer only.  The Runtime ledger remains the source of
truth; this module accepts one already validated, metadata-only terminal event
and never serializes its source payloads.
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from cowork_contracts import ComponentEventV1

from .keychain import KeychainError, parse_secret_reference
from .identity import is_pseudonym
from .observer_auth import ObserverAuthError, ObserverAuthorizationClient, ObserverTokenStore


_TERMINAL_STAGES = frozenset({"completed", "failed", "timeout", "cancelled"})
_RETRYABLE_STATUS_CODES = frozenset({408, 429})
_CONFIG_KEYS = (
    "AIHUB_TELEMETRY_CONSENT",
    "OBSERVER_AUTH_ORIGIN",
    "OBSERVER_AUTH_TOKEN_REF",
    "OBSERVER_CLIENT_TYPE",
    "OBSERVER_INSTALLATION_ID",
)
_SAFE_ATTRIBUTES = (
    "runtime",
    "plugin_id",
    "installation_id",
    "component_kind",
    "component_id",
    "component_version",
    "stage",
    "observation_kind",
    "evidence_source",
    "evidence_level",
    "source_type",
    "retry_count",
    "error_code",
    "input_tokens",
    "output_tokens",
    "total_tokens",
)


class LangfuseExportError(RuntimeError):
    """A Langfuse export failed and was not hidden from the caller."""

    def __init__(
        self,
        message: str,
        *,
        attempts: int = 0,
        status_code: int | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.status_code = status_code
        self.trace_id = trace_id
        self.span_id = span_id


@dataclass(frozen=True)
class LangfuseConfig:
    endpoint: str
    public_key: str
    secret_key: str
    observer_auth: ObserverAuthorizationClient | None = None
    timeout_seconds: float = 5.0
    max_attempts: int = 3
    retry_delay_seconds: float = 0.25

    @classmethod
    def from_env(cls) -> "LangfuseConfig":
        values: dict[str, str] = {}
        config_path = os.environ.get("LANGFUSE_CONFIG_FILE", "").strip()
        if config_path:
            try:
                lines = Path(config_path).expanduser().read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                raise LangfuseExportError(
                    f"Observer configuration file is unavailable: {type(exc).__name__}"
                ) from exc
            for line in lines:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, raw_value = stripped.split("=", 1)
                key = key.removeprefix("export ").strip()
                if key not in _CONFIG_KEYS:
                    continue
                value = raw_value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                    value = value[1:-1]
                values[key] = value.strip()
        for key in _CONFIG_KEYS:
            direct = os.environ.get(key, "").strip()
            if direct:
                values[key] = direct
        consent = values.get("AIHUB_TELEMETRY_CONSENT", "")
        if consent and consent != "granted":
            raise LangfuseExportError("telemetry consent is not granted")
        observer_ref = values.get("OBSERVER_AUTH_TOKEN_REF", "").strip()
        if not observer_ref:
            legacy_path = Path(config_path).expanduser().name if config_path else ""
            if legacy_path == "langfuse.env":
                raise LangfuseExportError(
                    "Observer authorization is required; the legacy credential configuration file is unsupported"
                )
            raise LangfuseExportError(
                "Observer authorization is required; run company-site authorization before starting the worker"
            )
        origin = values.get("OBSERVER_AUTH_ORIGIN", "https://itsub.loctek.com/observer").rstrip("/")
        client_type = values.get("OBSERVER_CLIENT_TYPE", "").strip()
        installation_id = values.get("OBSERVER_INSTALLATION_ID", "").strip()
        if not client_type or not installation_id:
            raise LangfuseExportError("Observer auth configuration requires client type and installation ID")
        try:
            scheme, service, account = parse_secret_reference(observer_ref)
            expected_scheme = "dpapi" if os.name == "nt" else "keychain"
            if scheme != expected_scheme:
                raise KeychainError("protected secret reference does not match this platform")
            auth = ObserverAuthorizationClient(
                origin=origin,
                client_type=client_type,
                installation_id=installation_id,
                store=ObserverTokenStore(service, account),
                refresh_lock_path=Path(config_path).expanduser().absolute().with_name(".observer-token-refresh.lock"),
            )
            token = auth.access_token()
        except (KeychainError, ObserverAuthError, ValueError) as exc:
            raise LangfuseExportError(f"Observer authorization is unavailable: {exc}") from exc
        return cls(endpoint=token.telemetry_endpoint, public_key="", secret_key="", observer_auth=auth)


@dataclass(frozen=True)
class LangfuseExportResult:
    attempts: int
    status_code: int
    trace_id: str
    span_id: str


def _uuid_hex(value: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_DNS, f"aihub-skill-mcp:{value}").hex


def _unix_nanos(value: datetime) -> int:
    normalized = value.astimezone(timezone.utc)
    return int(normalized.timestamp()) * 1_000_000_000 + normalized.microsecond * 1_000


def _attribute(key: str, value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": value}}
    if isinstance(value, str):
        return {"key": key, "value": {"stringValue": value}}
    raise LangfuseExportError(f"unsupported Langfuse attribute type: {key}")


def _payload(event: ComponentEventV1) -> tuple[dict[str, Any], str, str]:
    if event.stage not in _TERMINAL_STAGES:
        raise LangfuseExportError("Langfuse accepts terminal component events only")
    # Native DSH supplies a terminal fact without execution start. Langfuse
    # event observations are discrete; their OTLP edges timestamp the observation,
    # not a measured zero-duration execution. Keep other runtime projections intact.
    # https://langfuse.com/docs/observability/features/observation-types
    terminal_observation = event.runtime == "dsh" and event.started_at is None
    started_at = event.occurred_at if terminal_observation else event.started_at or event.occurred_at
    ended_at = event.occurred_at if terminal_observation else event.ended_at or event.occurred_at
    start_ns = _unix_nanos(started_at)
    end_ns = _unix_nanos(ended_at)
    if end_ns < start_ns:
        raise LangfuseExportError("Langfuse span end timestamp precedes start timestamp")

    trace_id = _uuid_hex(event.run_id)
    span_id = _uuid_hex(event.invocation_id)[:16]
    parent_span_id = _uuid_hex(event.parent_invocation_id)[:16] if event.parent_invocation_id else None
    attributes = [
        item
        for key in _SAFE_ATTRIBUTES
        if (item := _attribute(f"cowork.{key}", getattr(event, key))) is not None
    ]
    for key, value, prefix in (
        ("tenant_pseudonym", event.tenant_id, "tenant"),
        ("user_pseudonym", event.user_id, "user"),
    ):
        if is_pseudonym(value, prefix=prefix):
            attributes.append({"key": f"cowork.{key}", "value": {"stringValue": value}})
    # These values are safe scalar metadata and let Langfuse group the span.
    attributes.extend(
        [
            {"key": "langfuse.observation.type", "value": {"stringValue": "event" if terminal_observation else event.observation_kind}},
            {"key": "langfuse.trace.name", "value": {"stringValue": event.component_id}},
            {"key": "langfuse.session.id", "value": {"stringValue": event.run_id}},
            {"key": "cowork.original_run_id", "value": {"stringValue": event.run_id}},
            {"key": "cowork.original_invocation_id", "value": {"stringValue": event.invocation_id}},
        ]
    )
    catalog_ref = event.catalog_ref
    attributes.append(
        {
            "key": "cowork.skillhub.attribution_status",
            "value": {"stringValue": "explicit" if catalog_ref is not None else "unattributed"},
        }
    )
    if catalog_ref is not None:
        for key, value in (
            ("source_id", catalog_ref.source_id),
            ("version_id", catalog_ref.version_id),
            ("contract_version", catalog_ref.contract_version),
            ("ref_source", catalog_ref.ref_source),
        ):
            attributes.append(
                {"key": f"cowork.skillhub.{key}", "value": {"stringValue": value}}
            )
    if terminal_observation:
        attributes.extend([
            {"key": "cowork.execution_start_known", "value": {"boolValue": False}},
            {"key": "cowork.execution_duration_known", "value": {"boolValue": False}},
            {"key": "cowork.timestamp_semantics", "value": {"stringValue": "terminal_observation"}},
        ])
    # OTLP UNSET avoids asserting cancellation was successful completion. Timeout
    # is an unsuccessful operation (ERROR), retaining native stage/code attributes.
    # https://opentelemetry.io/docs/specs/otel/trace/api/#set-status
    status_code = (
        {"completed": 1, "failed": 2, "cancelled": 0, "timeout": 2}[event.stage]
        if event.runtime == "dsh" else (2 if event.stage == "failed" else 1)
    )
    for key, value in (
        ("langfuse.observation.input", event.user_input),
        ("langfuse.observation.output", event.model_output),
        ("cowork.tool_result", event.tool_result),
    ):
        if value is not None:
            attributes.append({"key": key, "value": {"stringValue": value}})
    span: dict[str, Any] = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": event.component_id,
        "kind": 1,
        "startTimeUnixNano": start_ns,
        "endTimeUnixNano": end_ns,
        "attributes": attributes,
        "status": {"code": status_code},
    }
    if parent_span_id:
        span["parentSpanId"] = parent_span_id
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "aihub-skill-mcp"}},
                        {"key": "service.version", "value": {"stringValue": "component_event.v1"}},
                    ]
                },
                "scopeSpans": [{"scope": {"name": "cowork-runtime"}, "spans": [span]}],
            }
        ]
    }, trace_id, span_id


class LangfuseExporter:
    """Export terminal ComponentEvent metadata over Langfuse OTLP/HTTP."""

    def __init__(self, config: LangfuseConfig) -> None:
        if config.max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if config.timeout_seconds <= 0 or config.retry_delay_seconds < 0:
            raise ValueError("Langfuse timeouts must be positive and retry delay non-negative")
        self.config = config

    def export(self, event: ComponentEventV1) -> LangfuseExportResult:
        body, trace_id, span_id = _payload(event)
        return self.export_body(body, trace_id=trace_id, span_id=span_id)

    def export_body(self, body: dict[str, Any], *, trace_id: str, span_id: str) -> LangfuseExportResult:
        """Send one already-built, metadata-only OTLP/JSON payload (e.g. a health record)."""
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if self.config.observer_auth is not None:
            try:
                auth_header, auth_value = self.config.observer_auth.authorization_header()
            except ObserverAuthError as exc:
                raise LangfuseExportError(str(exc), trace_id=trace_id, span_id=span_id) from exc
            headers = {auth_header: auth_value}
        else:
            auth = base64.b64encode(f"{self.config.public_key}:{self.config.secret_key}".encode()).decode("ascii")
            headers = {"Authorization": f"Basic {auth}"}
        last_status: int | None = None
        for attempt in range(1, self.config.max_attempts + 1):
            request = Request(
                self.config.endpoint,
                data=encoded,
                method="POST",
                headers={
                    **headers,
                    "Content-Type": "application/json",
                    "x-langfuse-ingestion-version": "4",
                },
            )
            try:
                with urlopen(request, timeout=self.config.timeout_seconds) as response:
                    status = int(response.status)
                if 200 <= status < 300:
                    return LangfuseExportResult(attempt, status, trace_id, span_id)
                last_status = status
                retryable = status in _RETRYABLE_STATUS_CODES or status >= 500
            except HTTPError as exc:
                last_status = int(exc.code)
                retryable = exc.code in _RETRYABLE_STATUS_CODES or exc.code >= 500
            except URLError:
                retryable = True
            except OSError:
                retryable = True
            if not retryable or attempt == self.config.max_attempts:
                break
            if self.config.retry_delay_seconds:
                import time

                time.sleep(self.config.retry_delay_seconds)
        raise LangfuseExportError(
            "Langfuse OTLP export failed",
            attempts=attempt,
            status_code=last_status,
            trace_id=trace_id,
            span_id=span_id,
        )


__all__ = [
    "LangfuseConfig",
    "LangfuseExportError",
    "LangfuseExportResult",
    "LangfuseExporter",
]
