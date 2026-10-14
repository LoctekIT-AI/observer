"""Durable SQLite telemetry outbox uploader."""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from cowork_contracts import AgentTelemetryIngestResponse


@dataclass(frozen=True)
class TelemetryUploadConfig:
    endpoint: str
    authorization: str
    producer: Mapping[str, Any]
    timeout_seconds: float = 5.0
    max_attempts: int = 8


@dataclass(frozen=True)
class TelemetryFlushReport:
    claimed: int
    delivered: int
    dead_letter: int
    retried: int


class TelemetryUploadError(RuntimeError):
    """The upload failed; the durable outbox state remains inspectable."""


class TelemetryUploader:
    def __init__(
        self,
        store: Any,
        config: TelemetryUploadConfig,
        *,
        random_source: Callable[[float, float], float] = random.uniform,
    ) -> None:
        if config.timeout_seconds <= 0 or config.max_attempts < 1:
            raise ValueError("telemetry uploader configuration is invalid")
        self.store = store
        self.config = config
        self.random_source = random_source

    def flush_once(self, *, worker_id: str, now: datetime, limit: int = 100) -> TelemetryFlushReport:
        claimed = self.store.claim_telemetry_outbox(
            owner_id=worker_id,
            now=now,
            lease=timedelta(seconds=60),
            limit=min(limit, 100),
        )
        if not claimed:
            return TelemetryFlushReport(0, 0, 0, 0)
        batch = {
            "schema_version": "agent.telemetry.batch.v1",
            "batch_id": f"telemetry-batch-{uuid4().hex}",
            "sent_at": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "producer": dict(self.config.producer),
            "events": [item.event for item in claimed],
        }
        body = json.dumps(batch, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = Request(
            self.config.endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": self.config.authorization,
                "Content-Type": "application/json",
            },
        )
        retry_after_seconds: int | None = None
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                status = int(response.status)
                raw = response.read()
        except HTTPError as exc:
            status = int(exc.code)
            raw = b""
            retry_after_seconds = _parse_retry_after(exc.headers.get("Retry-After"), now)
        except (URLError, OSError) as exc:
            for item in claimed:
                self._record_retry(item, worker_id=worker_id, now=now, error_code="network_error")
            raise TelemetryUploadError("telemetry upload failed: network error") from exc
        if 200 <= status < 300:
            try:
                response = AgentTelemetryIngestResponse.model_validate(json.loads(raw.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                for item in claimed:
                    self._record_retry(item, worker_id=worker_id, now=now, error_code="invalid_ingest_response")
                raise TelemetryUploadError("telemetry upload failed: invalid ingest response") from exc
            by_id = {item.event_id: item for item in response.events}
            delivered = dead_letter = 0
            for item in claimed:
                result = by_id.get(item.event_id)
                if result is None:
                    self._record_retry(item, worker_id=worker_id, now=now, error_code="missing_event_result")
                    continue
                if result.status.value in {"accepted", "duplicate"}:
                    self.store.mark_telemetry_delivered(item.event_id, owner_id=worker_id, now=now)
                    delivered += 1
                else:
                    self.store.mark_telemetry_failed(
                        item.event_id,
                        owner_id=worker_id,
                        now=now,
                        error_code=str(result.rejection_code),
                        retry_delay=timedelta(0),
                        max_attempts=1,
                    )
                    dead_letter += 1
            return TelemetryFlushReport(len(claimed), delivered, dead_letter, 0)
        retryable = status in {408, 429} or status >= 500
        for item in claimed:
            if retryable:
                self._record_retry(
                    item,
                    worker_id=worker_id,
                    now=now,
                    error_code=f"http_{status}",
                    retry_after_seconds=retry_after_seconds,
                )
            else:
                self.store.mark_telemetry_failed(
                    item.event_id,
                    owner_id=worker_id,
                    now=now,
                    error_code=f"http_{status}",
                    retry_delay=timedelta(0),
                    max_attempts=1,
                )
        if retryable:
            raise TelemetryUploadError(f"telemetry upload failed: HTTP {status}")
        return TelemetryFlushReport(len(claimed), 0, len(claimed), 0)

    def _record_retry(
        self,
        item: Any,
        *,
        worker_id: str,
        now: datetime,
        error_code: str,
        retry_after_seconds: int | None = None,
    ) -> None:
        retry_cap = min(900, 5 * (2 ** max(0, item.attempt_count)))
        delay_seconds = (
            retry_after_seconds
            if retry_after_seconds is not None
            else self.random_source(0.0, float(retry_cap))
        )
        self.store.mark_telemetry_failed(
            item.event_id,
            owner_id=worker_id,
            now=now,
            error_code=error_code,
            retry_delay=timedelta(seconds=delay_seconds),
            max_attempts=self.config.max_attempts,
        )


def _parse_retry_after(value: str | None, now: datetime) -> int | None:
    """Parse Retry-After and cap it to the frozen 3600-second operational limit."""

    if not value:
        return None
    stripped = value.strip()
    try:
        seconds = int(stripped)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(stripped)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None or retry_at.utcoffset() is None:
            return None
        seconds = int((retry_at.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds())
    if seconds < 0:
        return 0
    return min(seconds, 3600)


__all__ = ["TelemetryFlushReport", "TelemetryUploadConfig", "TelemetryUploadError", "TelemetryUploader"]
