"""Crawler job checkpoint persistence ports and in-memory implementation."""

from __future__ import annotations

from copy import deepcopy
from threading import RLock
from typing import Any, Optional, Protocol


class CrawlerJobStore(Protocol):
    def get_crawl_job(self, crawl_job_id: str) -> Optional[dict[str, Any]]:
        ...

    def save_crawl_job(self, checkpoint: dict[str, Any]) -> dict[str, Any]:
        ...

    def delete_crawl_job(self, crawl_job_id: str) -> None:
        ...


class InMemoryCrawlerJobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = RLock()

    def get_crawl_job(self, crawl_job_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(crawl_job_id)
            return deepcopy(job) if job is not None else None

    def save_crawl_job(self, checkpoint: dict[str, Any]) -> dict[str, Any]:
        crawl_job_id = _crawl_job_id_from_checkpoint(checkpoint)
        with self._lock:
            stored = deepcopy(checkpoint)
            self._jobs[crawl_job_id] = stored
            return deepcopy(stored)

    def delete_crawl_job(self, crawl_job_id: str) -> None:
        with self._lock:
            self._jobs.pop(crawl_job_id, None)


def _crawl_job_id_from_checkpoint(checkpoint: dict[str, Any]) -> str:
    crawl_job_id = str(checkpoint.get("crawl_job_id") or "").strip()
    if not crawl_job_id:
        raise ValueError("crawler checkpoint requires crawl_job_id")
    return crawl_job_id
