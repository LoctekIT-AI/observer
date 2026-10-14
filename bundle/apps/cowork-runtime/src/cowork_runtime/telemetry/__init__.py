"""Runtime tracing, projection, and redaction helpers with lazy exports."""

from __future__ import annotations

from importlib import import_module


_EXPORTS = {
    "EventStoreTraceEmitter": ("emitter", "EventStoreTraceEmitter"),
    "TraceEmitter": ("emitter", "TraceEmitter"),
    "project_causal_graph": ("causal_projection", "project_causal_graph"),
    "validate_causal_graph": ("causal_projection", "validate_causal_graph"),
    "TraceAccessRole": ("governance", "TraceAccessRole"),
    "TraceAuditEntry": ("governance", "TraceAuditEntry"),
    "TraceAuditLog": ("governance", "TraceAuditLog"),
    "TraceDataClass": ("governance", "TraceDataClass"),
    "TraceGovernanceError": ("governance", "TraceGovernanceError"),
    "TraceGovernancePolicy": ("governance", "TraceGovernancePolicy"),
    "TraceRetentionTier": ("governance", "TraceRetentionTier"),
    "authorize_trace_access": ("governance", "authorize_trace_access"),
    "evaluate_trace_access": ("governance", "evaluate_trace_access"),
    "redact_event_for_role": ("governance", "redact_event_for_role"),
    "redact_trace_export": ("governance", "redact_trace_export"),
    "project_orchestrated_trace_v2": ("orchestration_projection", "project_orchestrated_trace_v2"),
    "RUNTIME_TRACE_SCHEMA_VERSION": ("projection", "RUNTIME_TRACE_SCHEMA_VERSION"),
    "project_runtime_trace_v1": ("projection", "project_runtime_trace_v1"),
    "redact_sensitive": ("redaction", "redact_sensitive"),
    "trace_event_payload": ("redaction", "trace_event_payload"),
    "RetentionAction": ("retention", "RetentionAction"),
    "RetentionBackend": ("retention", "RetentionBackend"),
    "RetentionItem": ("retention", "RetentionItem"),
    "RetentionPlan": ("retention", "RetentionPlan"),
    "RetentionPolicy": ("retention", "RetentionPolicy"),
    "plan_retention_cleanup": ("retention", "plan_retention_cleanup"),
    "LangfuseConfig": ("langfuse", "LangfuseConfig"),
    "LangfuseExportError": ("langfuse", "LangfuseExportError"),
    "LangfuseExportResult": ("langfuse", "LangfuseExportResult"),
    "LangfuseExporter": ("langfuse", "LangfuseExporter"),
    "KeychainError": ("keychain", "KeychainError"),
    "read_secret": ("keychain", "read_secret"),
    "store_secret": ("keychain", "store_secret"),
    "LangfuseContentOutbox": ("langfuse_outbox", "LangfuseContentOutbox"),
    "LangfuseDeliveryStatus": ("langfuse_outbox", "LangfuseDeliveryStatus"),
    "LangfuseDeliveryWorker": ("langfuse_outbox", "LangfuseDeliveryWorker"),
    "LangfuseFlushReport": ("langfuse_outbox", "LangfuseFlushReport"),
    "LangfuseOutboxItem": ("langfuse_outbox", "LangfuseOutboxItem"),
    "TelemetryFlushReport": ("uploader", "TelemetryFlushReport"),
    "TelemetryUploadConfig": ("uploader", "TelemetryUploadConfig"),
    "TelemetryUploadError": ("uploader", "TelemetryUploadError"),
    "TelemetryUploader": ("uploader", "TelemetryUploader"),
    "TelemetrySensitiveFieldError": ("sdk", "TelemetrySensitiveFieldError"),
    "build_event": ("sdk", "build_event"),
    "canonical_payload_hash": ("sdk", "canonical_payload_hash"),
    "redact_for_telemetry": ("sdk", "redact_for_telemetry"),
    "project_runtime_trace_v2": ("span_projection", "project_runtime_trace_v2"),
    "SEMANTIC_TRACE_SCHEMA_VERSION": ("semantic_trace", "SEMANTIC_TRACE_SCHEMA_VERSION"),
    "SemanticTraceError": ("semantic_trace", "SemanticTraceError"),
    "project_semantic_trace": ("semantic_trace", "project_semantic_trace"),
    "ObserverIdentity": ("identity", "ObserverIdentity"),
    "is_pseudonym": ("identity", "is_pseudonym"),
    "validate_pseudonym": ("identity", "validate_pseudonym"),
    "ObserverAuthError": ("observer_auth", "ObserverAuthError"),
    "ObserverAuthorizationClient": ("observer_auth", "ObserverAuthorizationClient"),
    "ObserverToken": ("observer_auth", "ObserverToken"),
    "ObserverTokenStore": ("observer_auth", "ObserverTokenStore"),
    "pkce_pair": ("observer_auth", "pkce_pair"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(f"{__name__}.{module_name}"), attribute)
    globals()[name] = value
    return value
