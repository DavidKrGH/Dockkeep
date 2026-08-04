"""Central AppData SQLite schema manager for the stats model generation."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

SCHEMA_GENERATION_KEY = "schema_generation"
SCHEMA_GENERATION = "run_mode_v1"

SQLITE_TIMEOUT_SECONDS = 5.0
SQLITE_BUSY_TIMEOUT_MS = 5000

RUN_STATUSES = (
    "running",
    "success",
    "failed",
    "cancelled",
    "skipped",
    "lock_error",
    "config_error",
    "unexpected_error",
)
RUN_KINDS = ("job_task", "restore")
STATS_TRIGGERS = ("backup", "cleanup", "refresh")

TARGET_TABLES = (
    "repositories",
    "repository_locations",
    "runs",
    "run_steps",
    "run_restores",
    "artifacts",
    "artifact_locations",
    "repository_stats_points",
)

TARGET_COLUMNS = {
    "repositories": (
        "repository_id",
        "backend",
        "backend_repository_id",
    ),
    "repository_locations": (
        "location_id",
        "repository_id",
        "repository_location_key",
        "display_repository",
    ),
    "runs": (
        "run_id",
        "origin",
        "run_kind",
        "job",
        "task_type",
        "task_name",
        "started_at",
        "finished_at",
        "status",
        "error",
        "dry_run",
    ),
    "run_steps": (
        "run_step_id",
        "run_id",
        "position",
        "step",
        "backend",
        "task_type",
        "task_name",
        "started_at",
        "finished_at",
        "status",
        "error",
        "effective_task_config_json",
    ),
    "run_restores": (
        "run_restore_id",
        "run_id",
        "job",
        "backup",
        "backend",
        "snapshot_id",
        "mode",
        "restore_target",
        "snapshot_paths_json",
        "include_patterns_json",
        "exclude_patterns_json",
        "overwrite",
        "error",
        "output",
        "output_truncated",
    ),
    "artifacts": (
        "artifact_id",
        "repository_id",
        "backend",
        "backend_artifact_id",
        "backend_artifact_short_id",
        "created_at",
        "created_run_step_id",
        "hostname",
        "backup_start",
        "backup_end",
        "duration_seconds",
        "files_new",
        "files_changed",
        "files_unmodified",
        "dirs_new",
        "dirs_changed",
        "dirs_unmodified",
        "data_added_bytes",
        "data_added_packed_bytes",
        "total_files_processed",
        "total_bytes_processed",
        "paths_json",
        "tags_json",
        "raw_snapshot_json",
        "raw_backup_output_json",
    ),
    "artifact_locations": (
        "artifact_location_id",
        "artifact_id",
        "location_id",
        "present",
        "removed_at",
        "removed_by_run_step_id",
    ),
    "repository_stats_points": (
        "stats_point_id",
        "repository_id",
        "location_id",
        "run_step_id",
        "trigger",
        "collected_at",
        "total_size_bytes",
        "artifacts_count",
        "raw_stats_json",
    ),
}

OLD_APPDATA_TABLES = (
    "backup_repositories",
    "backup_repository_locations",
    "backup_repository_contexts",
    "backup_artifacts",
    "backup_artifact_metadata",
    "backup_artifact_summaries",
    "backup_artifact_paths",
    "backup_artifact_tags",
    "backup_artifact_location_observations",
    "backup_artifact_context_observations",
    "backup_repository_stats_points",
    "backup_artifact_creation_runs",
    "runs",
    "restores",
)

_INIT_LOCK = threading.Lock()


def utc_rfc3339(value: datetime) -> str:
    """Return ``value`` as a UTC-normalized RFC3339 string with a literal "Z" suffix."""
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_appdata_datetime(value: Any) -> datetime | None:
    """Parse an AppData timestamp and return a timezone-aware UTC datetime."""
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def connect_appdata_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        db_path,
        timeout=SQLITE_TIMEOUT_SECONDS,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    configure_connection(conn)
    ensure_appdata_schema(conn)
    return conn


@contextmanager
def immediate_tx(connect: Callable[[], sqlite3.Connection]) -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
    finally:
        conn.close()


def configure_connection(conn: sqlite3.Connection) -> None:
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA journal_mode = WAL").fetchone()
    except sqlite3.DatabaseError:
        pass


def ensure_appdata_schema(conn: sqlite3.Connection) -> None:
    with _INIT_LOCK:
        if not _has_current_generation(conn) or not _has_target_schema(conn):
            _reset_schema(conn)


def _has_current_generation(conn: sqlite3.Connection) -> bool:
    if not _table_exists(conn, "appdata_meta"):
        return False
    row = conn.execute(
        "SELECT value FROM appdata_meta WHERE key = ?",
        (SCHEMA_GENERATION_KEY,),
    ).fetchone()
    return row is not None and row["value"] == SCHEMA_GENERATION


def _has_target_schema(conn: sqlite3.Connection) -> bool:
    return all(
        _table_exists(conn, table) and _table_columns(conn, table) == columns
        for table, columns in TARGET_COLUMNS.items()
    )


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return tuple(str(row["name"]) for row in rows)


def _reset_schema(conn: sqlite3.Connection) -> None:
    previous_fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        with _transaction(conn):
            for table in _tables_to_drop(conn):
                conn.execute(f'DROP TABLE IF EXISTS "{table}"')
            _create_schema(conn)
            conn.execute(
                """
                INSERT INTO appdata_meta (key, value)
                VALUES (?, ?)
                """,
                (SCHEMA_GENERATION_KEY, SCHEMA_GENERATION),
            )
    finally:
        conn.execute(f"PRAGMA foreign_keys = {1 if previous_fk else 0}")


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    else:
        if conn.in_transaction:
            conn.execute("COMMIT")


def _tables_to_drop(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name NOT LIKE 'sqlite_%'
          AND (
              name LIKE 'backup_%'
              OR name IN ({drop_names})
          )
        """.format(
            drop_names=", ".join(
                f"'{table}'" for table in ("appdata_meta", *OLD_APPDATA_TABLES, *TARGET_TABLES)
            )
        )
    ).fetchall()
    return [str(row["name"]) for row in rows]


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(f"""
        CREATE TABLE appdata_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE repositories (
            repository_id TEXT PRIMARY KEY,
            backend TEXT NOT NULL,
            backend_repository_id TEXT NOT NULL,
            UNIQUE (backend, backend_repository_id)
        );

        CREATE TABLE repository_locations (
            location_id TEXT PRIMARY KEY,
            repository_id TEXT NOT NULL
                REFERENCES repositories(repository_id) ON DELETE CASCADE,
            repository_location_key TEXT NOT NULL UNIQUE,
            display_repository TEXT NOT NULL
        );

        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            origin TEXT NOT NULL,
            run_kind TEXT NOT NULL CHECK (run_kind IN {_sql_values(RUN_KINDS)}),
            job TEXT NOT NULL,
            task_type TEXT NOT NULL,
            task_name TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL CHECK (status IN {_sql_values(RUN_STATUSES)}),
            error TEXT,
            dry_run INTEGER NOT NULL CHECK (dry_run IN (0, 1))
        );

        CREATE TABLE run_steps (
            run_step_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL
                REFERENCES runs(run_id) ON DELETE CASCADE,
            position INTEGER NOT NULL CHECK (position >= 0),
            step TEXT NOT NULL,
            backend TEXT NOT NULL,
            task_type TEXT NOT NULL,
            task_name TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL CHECK (status IN {_sql_values(RUN_STATUSES)}),
            error TEXT,
            effective_task_config_json TEXT NOT NULL,
            UNIQUE (run_id, position)
        );

        CREATE TABLE run_restores (
            run_restore_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL
                REFERENCES runs(run_id) ON DELETE CASCADE,
            job TEXT NOT NULL,
            backup TEXT NOT NULL,
            backend TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            mode TEXT NOT NULL,
            restore_target TEXT NOT NULL,
            snapshot_paths_json TEXT NOT NULL,
            include_patterns_json TEXT NOT NULL,
            exclude_patterns_json TEXT NOT NULL,
            overwrite INTEGER NOT NULL CHECK (overwrite IN (0, 1)),
            error TEXT,
            output TEXT,
            output_truncated INTEGER NOT NULL CHECK (output_truncated IN (0, 1)),
            UNIQUE (run_id)
        );

        CREATE TABLE artifacts (
            artifact_id TEXT PRIMARY KEY,
            repository_id TEXT NOT NULL
                REFERENCES repositories(repository_id) ON DELETE CASCADE,
            backend TEXT NOT NULL,
            backend_artifact_id TEXT NOT NULL,
            backend_artifact_short_id TEXT,
            created_at TEXT NOT NULL,
            created_run_step_id TEXT
                REFERENCES run_steps(run_step_id) ON DELETE SET NULL,
            hostname TEXT,
            backup_start TEXT,
            backup_end TEXT,
            duration_seconds REAL CHECK (
                duration_seconds IS NULL OR duration_seconds >= 0
            ),
            files_new INTEGER CHECK (files_new IS NULL OR files_new >= 0),
            files_changed INTEGER CHECK (files_changed IS NULL OR files_changed >= 0),
            files_unmodified INTEGER CHECK (
                files_unmodified IS NULL OR files_unmodified >= 0
            ),
            dirs_new INTEGER CHECK (dirs_new IS NULL OR dirs_new >= 0),
            dirs_changed INTEGER CHECK (dirs_changed IS NULL OR dirs_changed >= 0),
            dirs_unmodified INTEGER CHECK (
                dirs_unmodified IS NULL OR dirs_unmodified >= 0
            ),
            data_added_bytes INTEGER CHECK (
                data_added_bytes IS NULL OR data_added_bytes >= 0
            ),
            data_added_packed_bytes INTEGER CHECK (
                data_added_packed_bytes IS NULL OR data_added_packed_bytes >= 0
            ),
            total_files_processed INTEGER CHECK (
                total_files_processed IS NULL OR total_files_processed >= 0
            ),
            total_bytes_processed INTEGER CHECK (
                total_bytes_processed IS NULL OR total_bytes_processed >= 0
            ),
            paths_json TEXT NOT NULL,
            tags_json TEXT NOT NULL,
            raw_snapshot_json TEXT NOT NULL,
            raw_backup_output_json TEXT,
            UNIQUE (repository_id, backend_artifact_id)
        );

        CREATE TABLE artifact_locations (
            artifact_location_id TEXT PRIMARY KEY,
            artifact_id TEXT NOT NULL
                REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
            location_id TEXT NOT NULL
                REFERENCES repository_locations(location_id) ON DELETE CASCADE,
            present INTEGER NOT NULL CHECK (present IN (0, 1)),
            removed_at TEXT,
            removed_by_run_step_id TEXT
                REFERENCES run_steps(run_step_id) ON DELETE SET NULL,
            UNIQUE (artifact_id, location_id)
        );

        CREATE TABLE repository_stats_points (
            stats_point_id TEXT PRIMARY KEY,
            repository_id TEXT NOT NULL
                REFERENCES repositories(repository_id) ON DELETE CASCADE,
            location_id TEXT NOT NULL
                REFERENCES repository_locations(location_id) ON DELETE CASCADE,
            run_step_id TEXT
                REFERENCES run_steps(run_step_id) ON DELETE SET NULL,
            trigger TEXT NOT NULL CHECK (trigger IN {_sql_values(STATS_TRIGGERS)}),
            collected_at TEXT NOT NULL,
            total_size_bytes INTEGER CHECK (
                total_size_bytes IS NULL OR total_size_bytes >= 0
            ),
            artifacts_count INTEGER CHECK (
                artifacts_count IS NULL OR artifacts_count >= 0
            ),
            raw_stats_json TEXT NOT NULL
        );

        CREATE INDEX idx_run_steps_run_id ON run_steps (run_id);
        CREATE INDEX idx_run_restores_run_id ON run_restores (run_id);
        CREATE INDEX idx_artifact_locations_location_id
            ON artifact_locations (location_id);
        CREATE INDEX idx_repository_stats_points_location_id
            ON repository_stats_points (location_id);
        CREATE INDEX idx_repository_stats_points_collected_at
            ON repository_stats_points (collected_at);
    """)


def _sql_values(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{value}'" for value in values) + ")"
