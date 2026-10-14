"""Safe, idempotent import of Runtime history from a split local SQLite store.

Desktop owns the decision to invoke this compatibility migration before a
sidecar starts.  The Runtime owns the SQLite facts and performs the actual
transaction so no renderer, HTTP route, or server database is involved.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .sqlite_store import SQLiteRuntimeStore


# These tables contain durable facts needed to list a session and replay its
# conversation.  We deliberately do not copy scheduler leases/delegation work:
# those records are live execution coordination, not user history.
HISTORY_TABLES = (
    "runtime_runs",
    "runtime_turns",
    "runtime_checkpoints",
    "runtime_crawl_jobs",
    "runtime_orchestration_states",
    "runtime_session_messages",
    "runtime_session_artifacts",
    "runtime_session_crawler_datasets",
    "runtime_pinned_constraints",
    "runtime_compactions",
    "runtime_events",
    "runtime_tool_outputs",
    "runtime_artifact_metadata",
    "runtime_script_assets",
    "runtime_skill_records",
)


@dataclass(frozen=True)
class RuntimeHistoryMigrationReport:
    destination: str
    sources: list[str]
    imported_rows: dict[str, int] = field(default_factory=dict)
    skipped_tables: dict[str, str] = field(default_factory=dict)
    backup_path: str | None = None

    @property
    def imported_row_count(self) -> int:
        return sum(self.imported_rows.values())


def migrate_runtime_history(
    destination: str | Path,
    sources: Iterable[str | Path],
) -> RuntimeHistoryMigrationReport:
    """Merge previously split Runtime history without replacing destination facts.

    The destination schema is upgraded through the normal Runtime store first.
    Existing primary keys always win, imports are ``INSERT OR IGNORE``, and a
    pre-import SQLite backup is created only when new rows are available.
    """

    destination_path = Path(destination).expanduser().resolve()
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    # Reuse the Runtime's canonical schema migration before comparing tables.
    store = SQLiteRuntimeStore(destination_path)
    store.close()

    source_paths = _distinct_existing_sources(destination_path, sources)
    report = RuntimeHistoryMigrationReport(
        destination=str(destination_path),
        sources=[str(source) for source in source_paths],
    )
    if not source_paths:
        return report

    if not _has_new_history_rows(destination_path, source_paths):
        return report

    backup_path = _backup_destination(destination_path)
    imported_rows: dict[str, int] = {}
    skipped_tables: dict[str, str] = {}
    connection = sqlite3.connect(destination_path, timeout=10.0)
    try:
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA foreign_keys = ON")
        for index, source_path in enumerate(source_paths):
            alias = f"history_source_{index}"
            connection.execute(f"ATTACH DATABASE ? AS {alias}", (str(source_path),))
            try:
                connection.execute("BEGIN IMMEDIATE")
                for table in HISTORY_TABLES:
                    columns, reason = _compatible_columns(connection, alias, table)
                    if reason:
                        skipped_tables.setdefault(table, reason)
                        continue
                    quoted_columns = ", ".join(_quote_identifier(column) for column in columns)
                    connection.execute(
                        f"INSERT OR IGNORE INTO {_quote_identifier(table)} ({quoted_columns}) "
                        f"SELECT {quoted_columns} FROM {_quote_identifier(alias)}.{_quote_identifier(table)}"
                    )
                    inserted = int(connection.execute("SELECT changes()").fetchone()[0])
                    if inserted:
                        imported_rows[table] = imported_rows.get(table, 0) + inserted
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.execute(f"DETACH DATABASE {_quote_identifier(alias)}")
    finally:
        connection.close()

    return RuntimeHistoryMigrationReport(
        destination=str(destination_path),
        sources=[str(source) for source in source_paths],
        imported_rows=imported_rows,
        skipped_tables=skipped_tables,
        backup_path=str(backup_path),
    )


def _distinct_existing_sources(destination: Path, sources: Iterable[str | Path]) -> list[Path]:
    result: list[Path] = []
    for raw_source in sources:
        source = Path(raw_source).expanduser().resolve()
        if source == destination or not source.is_file() or source in result:
            continue
        result.append(source)
    return result


def _has_new_history_rows(destination: Path, sources: list[Path]) -> bool:
    connection = sqlite3.connect(destination, timeout=10.0)
    try:
        for index, source in enumerate(sources):
            alias = f"history_check_{index}"
            connection.execute(f"ATTACH DATABASE ? AS {alias}", (str(source),))
            try:
                for table in HISTORY_TABLES:
                    columns, reason = _compatible_columns(connection, alias, table)
                    if reason:
                        continue
                    primary_key_columns = [
                        name for name, _not_null, _default, primary_key in _table_columns(connection, "main", table)
                        if primary_key
                    ]
                    if not primary_key_columns:
                        continue
                    join_condition = " AND ".join(
                        f"destination.{_quote_identifier(column)} = source.{_quote_identifier(column)}"
                        for column in primary_key_columns
                    )
                    missing_condition = " AND ".join(
                        f"destination.{_quote_identifier(column)} IS NULL" for column in primary_key_columns
                    )
                    query = (
                        f"SELECT 1 FROM {_quote_identifier(alias)}.{_quote_identifier(table)} AS source "
                        f"LEFT JOIN {_quote_identifier(table)} AS destination ON {join_condition} "
                        f"WHERE {missing_condition} LIMIT 1"
                    )
                    if connection.execute(query).fetchone():
                        return True
            finally:
                connection.execute(f"DETACH DATABASE {_quote_identifier(alias)}")
    finally:
        connection.close()
    return False


def _compatible_columns(connection: sqlite3.Connection, source_alias: str, table: str) -> tuple[list[str], str | None]:
    destination_columns = _table_columns(connection, "main", table)
    source_columns = _table_columns(connection, source_alias, table)
    if not source_columns:
        return [], "source table is unavailable"
    if not destination_columns:
        return [], "destination table is unavailable"
    missing_required = [
        name for name, not_null, default, _primary_key in destination_columns
        if not_null and default is None and name not in {column[0] for column in source_columns}
    ]
    if missing_required:
        return [], f"source lacks required columns: {', '.join(missing_required)}"
    destination_names = {column[0] for column in destination_columns}
    return [name for name, _not_null, _default, _primary_key in source_columns if name in destination_names], None


def _table_names(connection: sqlite3.Connection, database: str) -> set[str]:
    rows = connection.execute(
        f"SELECT name FROM {_quote_identifier(database)}.sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _table_columns(connection: sqlite3.Connection, database: str, table: str) -> list[tuple[str, bool, object, bool]]:
    rows = connection.execute(
        f"PRAGMA {_quote_identifier(database)}.table_info({_quote_identifier(table)})"
    ).fetchall()
    return [(str(row[1]), bool(row[3]), row[4], bool(row[5])) for row in rows]


def _backup_destination(destination: Path) -> Path:
    backup_directory = destination.parent / "history-migration-backups"
    backup_directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_directory / f"{destination.stem}-before-history-import-{timestamp}.sqlite3"
    source = sqlite3.connect(destination, timeout=10.0)
    try:
        backup = sqlite3.connect(backup_path)
        try:
            source.backup(backup)
        finally:
            backup.close()
    finally:
        source.close()
    return backup_path


def _quote_identifier(value: str) -> str:
    # return f'"{value.replace("\"", "\"\"")}"'
    escaped = value.replace('"', '""')
    return f'"{escaped}"'


def _parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Merge split Cowork Runtime history into one SQLite store.")
    parser.add_argument("--destination", required=True)
    parser.add_argument("--source", action="append", default=[])
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = _parse_arguments(argv or sys.argv[1:])
    report = migrate_runtime_history(arguments.destination, arguments.source)
    print(json.dumps({**asdict(report), "imported_row_count": report.imported_row_count}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
