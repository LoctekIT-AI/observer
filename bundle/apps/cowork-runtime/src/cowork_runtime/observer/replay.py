"""Metadata replay after a collector update.

When a collector meets a native record it cannot read (a client update changed
its format), it reports one degraded ``unrecognized_call`` record and notes here
*where* in the native file that happened -- the file, the file identity and the
read position before the unreadable record. No content is kept: the client's own
file stays the only copy.

When a newer collector version starts (an update arrived, see
scripts/observer_update.py), every note made by an older version moves that
file's read position back to the noted position, so the new adapter reads those
records again. Event IDs are stable, so records that were already delivered are
not delivered twice; a record the new version can read is added next to the
degraded one. If the new version still cannot read it, it notes it again under
its own version and does not replay it again until the next update.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from ..telemetry.health import bundle_version


@dataclass(frozen=True)
class ReplayPoint:
    installation_id: str
    source_path: str
    file_identity: str
    offset: int
    guarded: bool  # the first read skipped records from before the installation


class ReplayLedger:
    def __init__(self, state_dir: Path, *, version: str | None = None) -> None:
        self.path = Path(state_dir) / "replay.json"
        self.version = version or bundle_version()

    def _load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        points = data.get("points") if isinstance(data, dict) else None
        return [item for item in points if isinstance(item, dict)] if isinstance(points, list) else []

    def _save(self, points: list[dict]) -> None:
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps({"points": points}, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.path)

    def note(self, installation_id: str, source_path: str, file_identity: str, offset: int, *, guarded: bool = False) -> None:
        """Remember the earliest unreadable position of one file for this version."""
        points = self._load()
        for item in points:
            if item.get("installation_id") == installation_id and item.get("source_path") == source_path:
                if item.get("file_identity") != file_identity or item.get("version") != self.version:
                    item.update(file_identity=file_identity, offset=offset, version=self.version, guarded=guarded)
                else:
                    item["offset"] = min(int(item.get("offset", offset)), offset)
                    item["guarded"] = bool(item.get("guarded")) or guarded
                break
        else:
            points.append({"installation_id": installation_id, "source_path": source_path, "file_identity": file_identity,
                "offset": offset, "version": self.version, "guarded": guarded})
        self._save(points)

    def take_due(self) -> list[ReplayPoint]:
        """Points noted by an older version; they are removed from the ledger."""
        points = self._load()
        due = [item for item in points if item.get("version") != self.version]
        if not due:
            return []
        self._save([item for item in points if item.get("version") == self.version])
        result = []
        for item in due:
            try:
                result.append(ReplayPoint(str(item["installation_id"]), str(item["source_path"]), str(item["file_identity"]),
                    max(0, int(item["offset"])), bool(item.get("guarded"))))
            except (KeyError, TypeError, ValueError):
                continue
        return result


def rewind(registry, point: ReplayPoint, *, current_identity: str | None) -> bool:
    """Move a stored read position back to a replay point. True when it moved."""
    from datetime import datetime, timezone

    from ..adapters.registry import CodexCursor

    if current_identity is None or current_identity != point.file_identity:
        return False  # the file is gone or was replaced; nothing to replay
    cursor = registry.cursor(point.installation_id, point.source_path)
    if cursor is None or cursor.file_identity != point.file_identity or cursor.offset <= point.offset:
        return False
    registry.save_cursor(CodexCursor(installation_id=point.installation_id, source_path=point.source_path,
        file_identity=point.file_identity, offset=point.offset, updated_at=datetime.now(timezone.utc)))
    return True


def apply_due(ledger: ReplayLedger, registry, identity) -> tuple[int, set[str]]:
    """Rewind every file noted by an older version. Returns (rewound, guarded paths)."""
    rewound, guarded = 0, set()
    for point in ledger.take_due():
        path = Path(point.source_path)
        try:
            current = identity(path) if path.is_file() else None
        except OSError:
            current = None
        if rewind(registry, point, current_identity=current):
            rewound += 1
            if point.guarded:
                guarded.add(point.source_path)
    return rewound, guarded


__all__ = ["ReplayLedger", "ReplayPoint", "apply_due", "rewind"]
