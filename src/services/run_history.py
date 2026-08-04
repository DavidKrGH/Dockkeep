"""SQLite-backed history store for terminal operational runs."""

import asyncio
import json
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .appdata_schema import connect_appdata_db, parse_appdata_datetime, utc_rfc3339
from .run_manager import RunKind, RunOrigin, RunRecord, RunStatus

logger = logging.getLogger(__name__)

_ACTIVE_STATUSES = {RunStatus.QUEUED, RunStatus.RUNNING}
INTERRUPTED_RUN_ERROR = "Run was interrupted by process restart"
APPDATA_RETENTION_DAYS_ENV = "DK_APPDATA_RETENTION_DAYS"
APPDATA_RETENTION_COUNT_ENV = "DK_APPDATA_RETENTION_COUNT"
DEFAULT_APPDATA_RETENTION_DAYS: int | None = None
DEFAULT_APPDATA_RETENTION_COUNT: int | None = None
REDACTED_PASSWORD = "******"


def default_appdata_db_path() -> Path:
    return Path(os.environ.get("DK_APPDATA_DIR", "/appdata")) / "appdata.db"


def appdata_retention_days() -> int | None:
    return _env_positive_int(
        APPDATA_RETENTION_DAYS_ENV,
        DEFAULT_APPDATA_RETENTION_DAYS,
        "days",
    )


def appdata_retention_count() -> int | None:
    return _env_positive_int(
        APPDATA_RETENTION_COUNT_ENV,
        DEFAULT_APPDATA_RETENTION_COUNT,
        "entries",
    )


class RunHistoryService:

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or default_appdata_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._retention_days = appdata_retention_days()
        self._retention_count = appdata_retention_count()

    async def record(self, record: RunRecord) -> None:
        """Persist a terminal run record.

        Active records are intentionally ignored because they are process-local
        state and would become orphaned after restart.
        """
        if record.status in _ACTIVE_STATUSES:
            return
        await self.finish_run(record)

    async def create_run(self, record: RunRecord) -> None:
        if record.started_at is None:
            return
        try:
            await asyncio.to_thread(self._create_run_sync, record)
        except Exception:
            logger.exception("Failed to create run history entry: %s", record.run_id)

    async def finish_run(self, record: RunRecord) -> None:
        """Mark a persisted parent run terminal, creating it first if needed."""
        try:
            await asyncio.to_thread(self._finish_run_sync, record)
        except Exception:
            logger.exception("Failed to finish run history entry: %s", record.run_id)

    async def create_step(
        self,
        *,
        run_id: str,
        step: str,
        backend: str,
        task_type: str,
        task_name: str,
        effective_task_config: object,
    ) -> str:
        run_step_id = str(uuid4())
        try:
            await asyncio.to_thread(
                self._create_step_sync,
                run_step_id,
                run_id,
                step,
                backend,
                task_type,
                task_name,
                _compact_json(effective_task_config),
            )
        except Exception:
            logger.exception("Failed to create run step for run %s: %s", run_id, step)
        return run_step_id

    async def finish_step(
        self,
        run_step_id: str,
        *,
        status: RunStatus | str,
        error: str | None = None,
    ) -> None:
        """Mark a run-step row terminal."""
        try:
            await asyncio.to_thread(self._finish_step_sync, run_step_id, status, error)
        except Exception:
            logger.exception("Failed to finish run step: %s", run_step_id)

    async def get(self, run_id: str) -> RunRecord | None:
        try:
            return await asyncio.to_thread(self._get_sync, run_id)
        except Exception:
            logger.exception("Failed to read run history entry: %s", run_id)
            return None

    async def list_history(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
        exclude_run_ids: set[str] | None = None,
        job: str | None = None,
        task: str | None = None,
        status: str | None = None,
        origin: str | None = None,
    ) -> list[RunRecord]:
        try:
            return await asyncio.to_thread(
                self._list_history_sync,
                limit,
                offset,
                exclude_run_ids,
                job,
                task,
                status,
                origin,
            )
        except Exception:
            logger.exception("Failed to list run history entries")
            return []

    async def list_filter_values(self) -> dict[str, list[str]]:
        try:
            return await asyncio.to_thread(self._list_filter_values_sync)
        except Exception:
            logger.exception("Failed to list run history filter values")
            return {"jobs": [], "tasks": [], "statuses": [], "origins": []}

    async def count_failures_since(self, *, since: datetime) -> int:
        try:
            return await asyncio.to_thread(self._count_failures_since_sync, since)
        except Exception:
            logger.exception("Failed to count failed run history entries")
            return 0

    async def list_finished_between(
        self,
        *,
        after: datetime,
        before_or_at: datetime,
    ) -> list[RunRecord]:
        """Return terminal runs whose ``finished_at`` is in ``(after, before_or_at]``.

        The periodic notification report uses this as a pure read view over
        persisted history. Active rows and interrupted/incomplete rows without a
        ``finished_at`` timestamp are intentionally excluded.
        """
        try:
            return await asyncio.to_thread(
                self._list_finished_between_sync,
                after,
                before_or_at,
            )
        except Exception:
            logger.exception("Failed to list run history entries for report window")
            return []

    async def mark_active_runs_interrupted(self, *, origins: set[RunOrigin] | None = None) -> int:
        """Mark stale active history rows as terminal after a runtime restart."""
        try:
            return await asyncio.to_thread(self.mark_active_runs_interrupted_sync, origins=origins)
        except Exception:
            logger.exception("Failed to mark stale active run history entries interrupted")
            return 0

    def mark_active_runs_interrupted_sync(self, *, origins: set[RunOrigin] | None = None) -> int:
        """Synchronously mark stale active history rows as interrupted.

        Startup callers use this before exposing historical runs. ``origins``
        scopes the sweep to the runtime owner so an unrelated live scheduler
        process is not terminalized by a GUI or CLI startup path.
        """
        try:
            with closing(self._connect()) as conn:
                return self._mark_active_runs_interrupted_sync(conn, origins=origins)
        except Exception:
            logger.exception("Failed to mark stale active run history entries interrupted")
            return 0

    def _connect(self) -> sqlite3.Connection:
        return connect_appdata_db(self.db_path)

    def _create_run_sync(self, record: RunRecord) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, origin, run_kind, job, task_type, task_name,
                    started_at, finished_at, status, error, dry_run
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    origin = excluded.origin,
                    run_kind = excluded.run_kind,
                    job = excluded.job,
                    task_type = excluded.task_type,
                    task_name = excluded.task_name,
                    started_at = excluded.started_at,
                    status = 'running',
                    dry_run = excluded.dry_run,
                    finished_at = NULL,
                    error = NULL
                """,
                _record_to_running_row(record),
            )

    def _finish_run_sync(self, record: RunRecord) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, origin, run_kind, job, task_type, task_name,
                    started_at, finished_at, status, error, dry_run
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    origin = excluded.origin,
                    run_kind = excluded.run_kind,
                    job = excluded.job,
                    task_type = excluded.task_type,
                    task_name = excluded.task_name,
                    status = excluded.status,
                    dry_run = excluded.dry_run,
                    started_at = excluded.started_at,
                    finished_at = excluded.finished_at,
                    error = excluded.error
                """,
                _record_to_terminal_row(record),
            )
            self._prune_sync(conn)

    def _create_step_sync(
        self,
        run_step_id: str,
        run_id: str,
        step: str,
        backend: str,
        task_type: str,
        task_name: str,
        effective_task_config_json: str,
    ) -> None:
        with closing(self._connect()) as conn:
            position = _next_step_position(conn, run_id)
            now = utc_rfc3339(datetime.now(timezone.utc))
            conn.execute(
                """
                INSERT INTO run_steps (
                    run_step_id, run_id, position, step, backend, task_type,
                    task_name, started_at, finished_at, status, error,
                    effective_task_config_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?)
                """,
                (
                    run_step_id,
                    run_id,
                    position,
                    step,
                    backend,
                    task_type,
                    task_name,
                    now,
                    RunStatus.RUNNING.value,
                    effective_task_config_json,
                ),
            )

    def _finish_step_sync(
        self, run_step_id: str, status: RunStatus | str, error: str | None
    ) -> None:
        status_value = status.value if isinstance(status, RunStatus) else str(status)
        with closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE run_steps
                SET status = ?, finished_at = ?, error = ?
                WHERE run_step_id = ?
                """,
                (status_value, utc_rfc3339(datetime.now(timezone.utc)), error, run_step_id),
            )

    def _get_sync(self, run_id: str) -> RunRecord | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT run_id, origin, run_kind, job, task_type, task_name,
                       started_at, finished_at, status, error, dry_run
                FROM runs
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            return _row_to_record(row)
        except ValueError:
            logger.exception("Skipping invalid run history entry: %s", run_id)
            return None

    def _list_history_sync(
        self,
        limit: int | None,
        offset: int = 0,
        exclude_run_ids: set[str] | None = None,
        job: str | None = None,
        task: str | None = None,
        status: str | None = None,
        origin: str | None = None,
    ) -> list[RunRecord]:
        if limit is not None and limit <= 0:
            return []
        offset = max(offset, 0)
        params: list[object] = []
        where_clauses: list[str] = []
        if exclude_run_ids:
            placeholders = ", ".join("?" for _ in exclude_run_ids)
            where_clauses.append(f"run_id NOT IN ({placeholders})")
            params.extend(sorted(exclude_run_ids))
        if job:
            where_clauses.append("job = ?")
            params.append(job)
        if task:
            where_clauses.append("(task_type || '.' || task_name) = ?")
            params.append(task)
        if status:
            where_clauses.append("status = ?")
            params.append(status)
        if origin:
            where_clauses.append("origin = ?")
            params.append(origin)
        where_clause = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        limit_clause = ""
        if limit is not None:
            limit_clause = "LIMIT ? OFFSET ?"
            params.extend((limit, offset))
        elif offset:
            limit_clause = "LIMIT -1 OFFSET ?"
            params.append(offset)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT run_id, origin, run_kind, job, task_type, task_name,
                       started_at, finished_at, status, error, dry_run
                FROM runs
                {where_clause}
                ORDER BY COALESCE(finished_at, started_at) DESC, run_id DESC
                {limit_clause}
                """,
                params,
            ).fetchall()

        records: list[RunRecord] = []
        for row in rows:
            try:
                records.append(_row_to_record(row))
            except ValueError:
                logger.exception("Skipping invalid run history entry: %s", row["run_id"])
        return records

    def _list_filter_values_sync(self) -> dict[str, list[str]]:
        with closing(self._connect()) as conn:
            jobs = _distinct_text_values(conn, "job")
            tasks = _distinct_task_values(conn)
            statuses = _distinct_text_values(conn, "status")
            origins = _distinct_text_values(conn, "origin")
        return {"jobs": jobs, "tasks": tasks, "statuses": statuses, "origins": origins}

    def _count_failures_since_sync(self, since: datetime) -> int:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*)
                FROM runs
                WHERE finished_at IS NOT NULL
                  AND finished_at > ?
                  AND status IN (?, ?)
                """,
                (
                    utc_rfc3339(since),
                    RunStatus.FAILED.value,
                    RunStatus.UNEXPECTED_ERROR.value,
                ),
            ).fetchone()
        return int(row[0])

    def _list_finished_between_sync(
        self,
        after: datetime,
        before_or_at: datetime,
    ) -> list[RunRecord]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT run_id, origin, run_kind, job, task_type, task_name,
                       started_at, finished_at, status, error, dry_run
                FROM runs
                WHERE finished_at IS NOT NULL
                  AND finished_at > ?
                  AND finished_at <= ?
                ORDER BY finished_at ASC, run_id ASC
                """,
                (utc_rfc3339(after), utc_rfc3339(before_or_at)),
            ).fetchall()

        records: list[RunRecord] = []
        for row in rows:
            try:
                records.append(_row_to_record(row))
            except ValueError:
                logger.exception("Skipping invalid run history entry: %s", row["run_id"])
        return records

    def _mark_active_runs_interrupted_sync(
        self, conn: sqlite3.Connection, *, origins: set[RunOrigin] | None
    ) -> int:
        now = utc_rfc3339(datetime.now(timezone.utc))
        active_values = tuple(status.value for status in _ACTIVE_STATUSES)
        params: list[object] = [
            RunStatus.UNEXPECTED_ERROR.value,
            now,
            INTERRUPTED_RUN_ERROR,
            *active_values,
        ]
        origin_clause = ""
        if origins is not None:
            origin_values = tuple(origin.value for origin in origins)
            if not origin_values:
                return 0
            origin_placeholders = ", ".join("?" for _ in origin_values)
            origin_clause = f"AND origin IN ({origin_placeholders})"
            params.extend(origin_values)

        status_placeholders = ", ".join("?" for _ in active_values)
        cursor = conn.execute(
            f"""
            UPDATE runs
            SET status = ?, finished_at = ?, error = ?
            WHERE status IN ({status_placeholders})
            {origin_clause}
            """,
            params,
        )
        if cursor.rowcount:
            self._prune_sync(conn)
        return max(cursor.rowcount, 0)

    def _prune_sync(self, conn: sqlite3.Connection) -> None:
        if self._retention_days is None and self._retention_count is None:
            return
        active_statuses = _active_status_values()
        active_placeholders = _placeholders(active_statuses)
        if self._retention_days is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=self._retention_days)
            # The protection clause is an internal SQL fragment with no user input.
            # It keeps run rows that are still linked to creation artifacts.
            conn.execute(
                f"""
                DELETE FROM runs
                WHERE run_kind = ?
                AND status NOT IN ({active_placeholders})
                AND COALESCE(finished_at, started_at) < ?
                {_artifact_run_protection_clause(conn)}
                """,
                (RunKind.JOB_TASK.value, *active_statuses, utc_rfc3339(cutoff)),
            )
        if self._retention_count is not None:
            conn.execute(
                f"""
                DELETE FROM runs
                WHERE run_kind = ?
                AND status NOT IN ({active_placeholders})
                AND run_id NOT IN (
                    SELECT run_id
                    FROM runs
                    WHERE run_kind = ?
                    AND status NOT IN ({active_placeholders})
                    {_artifact_run_protection_clause(conn)}
                    ORDER BY COALESCE(finished_at, started_at) DESC, run_id DESC
                    LIMIT ?
                )
                {_artifact_run_protection_clause(conn)}
                """,
                (
                    RunKind.JOB_TASK.value,
                    *active_statuses,
                    RunKind.JOB_TASK.value,
                    *active_statuses,
                    self._retention_count,
                ),
            )


def _active_status_values() -> tuple[str, ...]:
    return tuple(sorted(status.value for status in _ACTIVE_STATUSES))


def _placeholders(values: tuple[object, ...]) -> str:
    return ", ".join("?" for _value in values)


def _record_to_running_row(
    record: RunRecord,
) -> tuple[str, str, str, str, str, str, str, None, str, None, int]:
    return (
        record.run_id,
        record.origin.value,
        record.run_kind.value,
        record.job,
        record.task_type,
        record.task_name,
        _datetime_to_text(record.started_at) or utc_rfc3339(record.created_at),
        None,
        RunStatus.RUNNING.value,
        None,
        int(record.dry_run),
    )


def _record_to_terminal_row(
    record: RunRecord,
) -> tuple[str, str, str, str, str, str, str, str | None, str, str | None, int]:
    return (
        record.run_id,
        record.origin.value,
        record.run_kind.value,
        record.job,
        record.task_type,
        record.task_name,
        _datetime_to_text(record.started_at) or utc_rfc3339(record.created_at),
        _datetime_to_text(record.finished_at),
        record.status.value,
        record.error,
        int(record.dry_run),
    )


def _next_step_position(conn: sqlite3.Connection, run_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 AS next_position FROM run_steps WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    return int(row["next_position"])


def _compact_json(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    value = _redact_persisted_config(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _redact_persisted_config(value: object) -> object:
    """Strip resolved passwords from a config snapshot before it is persisted.

    Scope is deliberately limited to the ``credentials`` block, which is the one
    place a resolved password lands in a config snapshot. Secrets a user embeds
    elsewhere by hand — inside a ``repository`` URL, an rclone endpoint or
    ``extra_*_args`` — are persisted verbatim: they are indistinguishable from
    ordinary values, and the AppData DB is local-only (see the "Lokaler Betrieb"
    invariant).
    """
    if isinstance(value, dict):
        return {
            key: (
                _redact_credentials(item)
                if key == "credentials"
                else _redact_persisted_config(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_persisted_config(item) for item in value]
    return value


def _redact_credentials(value: object) -> object:
    """Mask the resolved password, keeping the env-var/file metadata readable."""
    if not isinstance(value, dict):
        return value
    redacted = {key: _redact_persisted_config(item) for key, item in value.items()}
    if redacted.get("password_env"):
        redacted["password"] = None
    elif redacted.get("password_file"):
        redacted["password"] = None
    elif redacted.get("password") is not None:
        redacted["password"] = REDACTED_PASSWORD
    return redacted


def _artifact_run_protection_clause(conn: sqlite3.Connection) -> str:
    if not (
        _table_exists(conn, "artifacts")
        and _table_exists(conn, "artifact_locations")
        and _table_exists(conn, "run_steps")
    ):
        return ""
    return """
    AND (
        run_id IS NULL
        OR NOT EXISTS (
            SELECT 1
            FROM run_steps
            JOIN artifacts ON artifacts.created_run_step_id = run_steps.run_step_id
            JOIN artifact_locations
              ON artifact_locations.artifact_id = artifacts.artifact_id
             AND artifact_locations.present = 1
            WHERE run_steps.run_id = runs.run_id
        )
    )
    """


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _distinct_text_values(conn: sqlite3.Connection, column: str) -> list[str]:
    rows = conn.execute(f"""
        SELECT DISTINCT {column}
        FROM runs
        WHERE {column} IS NOT NULL AND {column} != ''
        ORDER BY {column} COLLATE NOCASE ASC
        """).fetchall()
    return [str(row[0]) for row in rows]


def _distinct_task_values(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("""
        SELECT DISTINCT task_type || '.' || task_name AS task
        FROM runs
        WHERE task_type IS NOT NULL AND task_type != ''
          AND task_name IS NOT NULL AND task_name != ''
        ORDER BY task COLLATE NOCASE ASC
        """).fetchall()
    return [str(row["task"]) for row in rows]


def _row_to_record(row: sqlite3.Row) -> RunRecord:
    origin = RunOrigin(str(row["origin"]))
    status = RunStatus(str(row["status"]))
    return RunRecord(
        run_id=str(row["run_id"]),
        origin=origin,
        run_kind=RunKind(str(row["run_kind"])),
        job=str(row["job"]),
        task_type=str(row["task_type"]),
        task_name=str(row["task_name"]),
        status=status,
        dry_run=bool(row["dry_run"]),
        cancellable=False,
        created_at=_datetime_from_text(row["started_at"]) or datetime.now(timezone.utc),
        started_at=_datetime_from_text(row["started_at"]),
        finished_at=_datetime_from_text(row["finished_at"]),
        error=str(row["error"]) if row["error"] is not None else None,
    )


def _datetime_to_text(value: datetime | None) -> str | None:
    return utc_rfc3339(value) if value is not None else None


def _datetime_from_text(value: Any) -> datetime | None:
    return parse_appdata_datetime(value)


def _env_positive_int(name: str, default: int | None, unit: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        if default is None:
            logger.warning(
                "Ignoring invalid %s=%r; expected a positive integer number of %s. "
                "Retention for this limit remains disabled.",
                name,
                raw,
                unit,
            )
        else:
            logger.warning(
                "Ignoring invalid %s=%r; expected a positive integer number of %s. "
                "Using default %s.",
                name,
                raw,
                unit,
                default,
            )
        return default
    return value
