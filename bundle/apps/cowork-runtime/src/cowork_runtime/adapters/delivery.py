"""Projection from adapter contracts into the existing Runtime event ledger."""

from __future__ import annotations

from collections.abc import Mapping

from cowork_contracts import COMPONENT_EVENT_CONTRACT_VERSION, ComponentEventV1
from cowork_runtime.contracts import RuntimeEvent
from cowork_runtime.contracts.enums import EventVisibility, RuntimeEventType
from cowork_runtime.telemetry.emitter import TraceEmitter

from .registry import PluginNotActiveError, PluginRegistry


_SAFE_EXTENSION_FIELDS = frozenset({
    "call_id",
    "tool_call_id",
    "tool_name",
    "server",
    "status",
    "visibility",
})


class AdapterDeliveryError(ValueError):
    """An adapter event is not eligible for Runtime ledger delivery."""


class ComponentEventDelivery:
    def __init__(self, emitter: TraceEmitter, registry: PluginRegistry) -> None:
        self.emitter = emitter
        self.registry = registry

    def deliver(
        self,
        event: ComponentEventV1,
        *,
        payload_extensions: Mapping[str, object] | None = None,
    ) -> RuntimeEvent:
        if event.schema_version != COMPONENT_EVENT_CONTRACT_VERSION:
            raise AdapterDeliveryError(
                f"unsupported component event contract: {event.schema_version}"
            )
        try:
            manifest = self.registry._require_active(event.installation_id)
        except PluginNotActiveError as exc:
            raise AdapterDeliveryError(str(exc)) from exc
        if manifest.contract_version != COMPONENT_EVENT_CONTRACT_VERSION:
            raise AdapterDeliveryError(
                f"adapter contract mismatch: {manifest.contract_version}"
            )
        if manifest.runtime != event.runtime:
            raise AdapterDeliveryError(
                f"adapter runtime mismatch: manifest={manifest.runtime}, event={event.runtime}"
            )
        if manifest.plugin_id != event.plugin_id:
            raise AdapterDeliveryError(
                f"adapter plugin mismatch: manifest={manifest.plugin_id}, event={event.plugin_id}"
            )

        binding = self.registry.binding(event.installation_id)
        payload = event.model_dump(mode="json")
        if payload_extensions:
            payload.update({
                key: value
                for key, value in payload_extensions.items()
                if key in _SAFE_EXTENSION_FIELDS
            })
        if binding is None:
            payload["tenant_id"] = None
            payload["user_id"] = None
        else:
            for field, expected in (("tenant_id", binding.tenant_id), ("user_id", binding.user_id)):
                actual = getattr(event, field)
                if actual is not None and actual != expected:
                    raise AdapterDeliveryError(f"component event {field} does not match installation binding")
                payload[field] = expected

        if event.stage == "started":
            event_type = RuntimeEventType.TOOL_CALL_CREATED
        elif event.stage in {"completed", "failed", "timeout", "cancelled"}:
            event_type = RuntimeEventType.TOOL_RESULT_CREATED
        else:  # pragma: no cover - ComponentEventV1 already constrains the literal.
            raise AdapterDeliveryError(f"unsupported component event stage: {event.stage}")
        return self.emitter.emit(
            event.run_id,
            event_type,
            payload,
            visibility=EventVisibility.USER,
            event_id=event.event_id,
            # ComponentEventV1 is already a strict metadata-only allowlist;
            # generic redaction would incorrectly redact numeric *_tokens
            # usage fields because their names contain the word "token".
            redact=False,
        )


__all__ = ["AdapterDeliveryError", "ComponentEventDelivery"]
