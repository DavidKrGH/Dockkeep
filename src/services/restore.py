"""Restore service backed by ``restic restore``."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

from ..core.locking import (
    JobAlreadyRunningError,
    ResourceLockManager,
    resource_for_repository,
)
from ..core.locking import (
    lock_dir as default_lock_dir,
)
from ..core.stream_logging import ByteTailBuffer, LineLogBuffer
from ..core.subprocesses import stream_command
from ..models.resolved_config import ResolvedAppConfig, ResolvedBackupConfig
from ..utils.logging import log_base_dir as default_log_base_dir
from ..utils.logging import log_task_context, setup_job_logger
from ..utils.restic import resolve_env
from .appdata_schema import connect_appdata_db, parse_appdata_datetime, utc_rfc3339
from .config import ConfigService, get_job_or_raise
from .errors import ConfigServiceError, NotFoundServiceError, ServiceError
from .output import limit_output
from .run_history import appdata_retention_count, appdata_retention_days, default_appdata_db_path
from .run_manager import RunKind, RunManager, RunOrigin, RunRecord, RunStatus

logger = logging.getLogger(__name__)

DEFAULT_RESTORE_BASE_DIR = Path("/restore")
RESTORE_TAIL_BYTES = 128 * 1024
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_SNAPSHOT_ID_RE = re.compile(r"^[A-Fa-f0-9]{8,64}$")
_RESTIC_GLOB_META_RE = re.compile(r"[*?[]")


class RestorePreviewView(TypedDict):
    ok: bool
    job: str
    backup: str
    snapshot_id: str
    mode: str
    snapshot_paths: list[str]
    include_patterns: list[str]
    exclude_patterns: list[str]
    restore_target: str
    overwrite: bool
    command: str
    argv: list[str]
    dry_run: bool
    output: str | None
    output_truncated: bool
    error: str | None


class RestoreView(TypedDict):
    run_id: str
    job: str
    backup: str
    snapshot_id: str
    mode: str
    snapshot_paths: list[str]
    include_patterns: list[str]
    exclude_patterns: list[str]
    restore_target: str
    overwrite: bool
    dry_run: bool
    status: str
    status_label: str
    status_tone: str
    is_active: bool
    is_cancellable: bool
    detail_url: str
    status_url: str
    cancel_url: str
    error: str | None
    output: str | None
    output_truncated: bool


class RestoreStatus(StrEnum):
    """Restore lifecycle status."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LOCK_ERROR = "lock_error"
    CONFIG_ERROR = "config_error"
    UNEXPECTED_ERROR = "unexpected_error"


class RestoreMode(StrEnum):
    """Supported restore selection modes."""

    PATTERN = "pattern"
    BROWSER = "browser"


_STATUS_LABELS = {
    RestoreStatus.QUEUED: "Queued",
    RestoreStatus.RUNNING: "Running",
    RestoreStatus.SUCCESS: "Successful",
    RestoreStatus.FAILED: "Failed",
    RestoreStatus.CANCELLED: "Cancelled",
    RestoreStatus.LOCK_ERROR: "Lock error",
    RestoreStatus.CONFIG_ERROR: "Config error",
    RestoreStatus.UNEXPECTED_ERROR: "Unexpected error",
}
_STATUS_TONES = {
    RestoreStatus.QUEUED: "amber",
    RestoreStatus.RUNNING: "blue",
    RestoreStatus.SUCCESS: "green",
    RestoreStatus.FAILED: "red",
    RestoreStatus.CANCELLED: "slate",
    RestoreStatus.LOCK_ERROR: "amber",
    RestoreStatus.CONFIG_ERROR: "red",
    RestoreStatus.UNEXPECTED_ERROR: "red",
}
_ACTIVE_STATUSES = {RestoreStatus.QUEUED, RestoreStatus.RUNNING}


@dataclass(frozen=True)
class RestoreRequest:
    """Validated restore request."""

    job: str
    backup: str
    snapshot_id: str
    mode: RestoreMode
    restore_target: Path
    snapshot_paths: tuple[str, ...] = ()
    include_patterns: tuple[str, ...] = ()
    exclude_patterns: tuple[str, ...] = ()
    overwrite: bool = False


@dataclass(frozen=True)
class RestoreCommand:
    """Command data for a restic restore invocation."""

    argv: list[str]
    restore_target: Path
    mode: RestoreMode
    snapshot_paths: tuple[str, ...]
    include_patterns: tuple[str, ...]
    exclude_patterns: tuple[str, ...]
    overwrite: bool
    dry_run: bool


@dataclass
class RestoreRecord:
    """In-memory restore record."""

    restore_id: str
    request: RestoreRequest
    status: RestoreStatus
    dry_run: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    output: str | None = None
    output_truncated: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    cancellable: bool = True


@dataclass(frozen=True)
class RestorePreview:
    """Dry-run restore result."""

    request: RestoreRequest
    command: RestoreCommand
    ok: bool
    output: str
    output_truncated: bool
    error: str | None = None


class RestoreRegistry:
    """SQLite-backed registry for restore detail records."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or default_appdata_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._retention_days = appdata_retention_days()
        self._retention_count = appdata_retention_count()

    async def add(self, record: RestoreRecord) -> None:
        """Add a restore detail record."""
        async with self._lock:
            await asyncio.to_thread(self._upsert_sync, record)

    async def get(self, restore_id: str) -> RestoreRecord | None:
        async with self._lock:
            return await asyncio.to_thread(self._get_sync, restore_id)

    async def update(self, restore_id: str, **changes: object) -> None:
        async with self._lock:
            record = await asyncio.to_thread(self._get_sync, restore_id)
            if record is None:
                raise KeyError(restore_id)
            for key, value in changes.items():
                setattr(record, key, value)
            await asyncio.to_thread(self._upsert_sync, record)

    async def discard(self, restore_id: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._discard_sync, restore_id)

    async def discard_run(self, restore_id: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._discard_run_sync, restore_id)

    def _connect(self) -> sqlite3.Connection:
        return connect_appdata_db(self.db_path)

    def _upsert_sync(self, record: RestoreRecord) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, origin, run_kind, job, task_type, task_name,
                    started_at, finished_at, status, error, dry_run
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    finished_at = excluded.finished_at,
                    status = excluded.status,
                    error = COALESCE(excluded.error, runs.error),
                    dry_run = excluded.dry_run
                """,
                (
                    record.restore_id,
                    RunOrigin.MANUAL.value,
                    RunKind.RESTORE.value,
                    record.request.job,
                    "restore",
                    record.request.backup,
                    _dt_to_text(record.started_at) or utc_rfc3339(record.created_at),
                    _dt_to_text(record.finished_at),
                    record.status.value,
                    record.error,
                    int(record.dry_run),
                ),
            )
            conn.execute(
                """
                INSERT INTO run_restores (
                    run_restore_id, run_id, job, backup, backend, snapshot_id, mode,
                    restore_target, snapshot_paths_json, include_patterns_json,
                    exclude_patterns_json, overwrite, error, output, output_truncated
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    job = excluded.job,
                    backup = excluded.backup,
                    backend = excluded.backend,
                    snapshot_id = excluded.snapshot_id,
                    mode = excluded.mode,
                    restore_target = excluded.restore_target,
                    snapshot_paths_json = excluded.snapshot_paths_json,
                    include_patterns_json = excluded.include_patterns_json,
                    exclude_patterns_json = excluded.exclude_patterns_json,
                    overwrite = excluded.overwrite,
                    error = excluded.error,
                    output = excluded.output,
                    output_truncated = excluded.output_truncated
                """,
                _restore_record_to_row(record),
            )
            self._prune_sync(conn)

    def _get_sync(self, restore_id: str) -> RestoreRecord | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT runs.run_id AS restore_id, runs.status, runs.dry_run,
                       runs.started_at, runs.finished_at, runs.error AS run_error,
                       run_restores.job, run_restores.backup, run_restores.snapshot_id,
                       run_restores.mode, run_restores.restore_target,
                       run_restores.snapshot_paths_json,
                       run_restores.include_patterns_json,
                       run_restores.exclude_patterns_json,
                       run_restores.overwrite, run_restores.error,
                       run_restores.output, run_restores.output_truncated
                FROM run_restores
                JOIN runs ON runs.run_id = run_restores.run_id
                WHERE run_restores.run_id = ?
                """,
                (restore_id,),
            ).fetchone()
        return _restore_row_to_record(row) if row is not None else None

    def _discard_sync(self, restore_id: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM run_restores WHERE run_id = ?", (restore_id,))

    def _discard_run_sync(self, restore_id: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "DELETE FROM runs WHERE run_id = ? AND run_kind = ?",
                (restore_id, RunKind.RESTORE.value),
            )

    def _prune_sync(self, conn: sqlite3.Connection) -> None:
        if self._retention_days is None and self._retention_count is None:
            return
        active = tuple(status.value for status in _ACTIVE_STATUSES)
        if self._retention_days is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=self._retention_days)
            conn.execute(
                """
                DELETE FROM runs
                WHERE run_kind = 'restore'
                  AND status NOT IN (?, ?)
                  AND COALESCE(finished_at, started_at) < ?
                """,
                (*active, utc_rfc3339(cutoff)),
            )
        if self._retention_count is not None:
            conn.execute(
                """
                DELETE FROM runs
                WHERE run_kind = 'restore'
                  AND status NOT IN (?, ?)
                  AND run_id NOT IN (
                      SELECT run_id
                      FROM runs
                      WHERE run_kind = 'restore'
                        AND status NOT IN (?, ?)
                      ORDER BY COALESCE(finished_at, started_at) DESC, run_id DESC
                      LIMIT ?
                  )
                """,
                (*active, *active, self._retention_count),
            )


class RestoreService:

    def __init__(
        self,
        config_service: ConfigService,
        registry: RestoreRegistry,
        run_manager: RunManager,
        *,
        restore_base_dir: Path | None = None,
        lock_dir: Path | None = None,
        log_base_dir: Path | None = None,
    ) -> None:
        self._config_service = config_service
        self._registry = registry
        self._run_manager = run_manager
        self._restore_base_dir = _resolve_restore_base(
            restore_base_dir or _default_restore_base_dir()
        )
        self._lock_dir = lock_dir if lock_dir is not None else default_lock_dir()
        self._log_base_dir = log_base_dir if log_base_dir is not None else default_log_base_dir()
        self._pending_restores: dict[str, RestoreRecord] = {}

    async def preview_restore(
        self,
        job_name: str,
        backup_name: str,
        snapshot_id: str,
        *,
        mode: str | RestoreMode,
        snapshot_paths: list[str] | tuple[str, ...] | None = None,
        include_patterns: list[str] | tuple[str, ...] | None = None,
        exclude_patterns: list[str] | tuple[str, ...] | None = None,
        target: str | Path | None = None,
        overwrite: bool = False,
        resolved_backup: ResolvedBackupConfig | None = None,
    ) -> RestorePreview:
        config = self._config_service.load_active_config()
        request, backup = self._request(
            config,
            job_name,
            backup_name,
            snapshot_id,
            mode,
            snapshot_paths,
            include_patterns,
            exclude_patterns,
            target,
            overwrite,
            resolved_backup=resolved_backup,
        )
        command = _build_restore_command(backup, request, dry_run=True)
        result = await _run_restore_command(
            command=command,
            backup=backup,
            job_name=request.job,
            log_level=config.global_.log_level,
            log_base_dir=self._log_base_dir,
            timeout=backup.timeouts.backup_timeout,
            restore_base_dir=self._restore_base_dir,
        )
        return RestorePreview(
            request=request,
            command=command,
            ok=result.ok,
            output=result.output,
            output_truncated=result.output_truncated,
            error=result.error,
        )

    async def start_restore(
        self,
        job_name: str,
        backup_name: str,
        snapshot_id: str,
        *,
        mode: str | RestoreMode,
        snapshot_paths: list[str] | tuple[str, ...] | None = None,
        include_patterns: list[str] | tuple[str, ...] | None = None,
        exclude_patterns: list[str] | tuple[str, ...] | None = None,
        target: str | Path | None = None,
        overwrite: bool = False,
        dry_run: bool = False,
        resolved_backup: ResolvedBackupConfig | None = None,
    ) -> RestoreRecord:
        config = self._config_service.load_active_config()
        request, backup = self._request(
            config,
            job_name,
            backup_name,
            snapshot_id,
            mode,
            snapshot_paths,
            include_patterns,
            exclude_patterns,
            target,
            overwrite,
            resolved_backup=resolved_backup,
        )
        restore_id = str(uuid4())
        record = RestoreRecord(restore_id, request, RestoreStatus.QUEUED, dry_run=dry_run)
        self._pending_restores[restore_id] = record
        try:
            run_record = await self._run_manager.start(
                RunOrigin.MANUAL,
                request.job,
                "restore",
                request.backup,
                lambda mark_not_cancellable: self._execute(
                    restore_id,
                    request,
                    backup,
                    config.global_.log_level,
                    dry_run,
                    mark_not_cancellable,
                ),
                run_kind=RunKind.RESTORE,
                dry_run=dry_run,
                run_id=restore_id,
            )
            running_record = replace(
                record,
                status=RestoreStatus.RUNNING,
                started_at=run_record.started_at or datetime.now(timezone.utc),
            )
            self._pending_restores[restore_id] = running_record
            await self._registry.add(running_record)
        except Exception:
            self._pending_restores.pop(restore_id, None)
            await self._registry.discard_run(restore_id)
            raise
        return record

    async def get_restore(self, run_id: str) -> RestoreRecord:
        record = await self._registry.get(run_id)
        if record is None:
            record = self._pending_restores.get(run_id)
            if record is None:
                raise ServiceError("restore_not_found", f"Restore not found: {run_id}", 404)
        await self._sync_run_record(record)
        return record

    async def cancel_restore(self, run_id: str) -> RestoreRecord:
        record = await self._registry.get(run_id)
        if record is None:
            record = self._pending_restores.get(run_id)
            if record is None:
                raise ServiceError("restore_not_found", f"Restore not found: {run_id}", 404)
        try:
            run_record = await self._run_manager.cancel(run_id)
        except NotFoundServiceError:
            return await self.get_restore(run_id)
        await self._sync_run_record(record, run_record)
        return record

    async def get_restore_view(self, run_id: str) -> RestoreView:
        return self.restore_view(await self.get_restore(run_id))

    def preview_view(self, preview: RestorePreview) -> RestorePreviewView:
        return {
            "ok": preview.ok,
            "job": preview.request.job,
            "backup": preview.request.backup,
            "snapshot_id": preview.request.snapshot_id,
            "mode": preview.request.mode.value,
            "snapshot_paths": list(preview.request.snapshot_paths),
            "include_patterns": list(preview.request.include_patterns),
            "exclude_patterns": list(preview.request.exclude_patterns),
            "restore_target": str(preview.request.restore_target),
            "overwrite": preview.request.overwrite,
            "command": " ".join(preview.command.argv),
            "argv": preview.command.argv,
            "dry_run": preview.command.dry_run,
            "output": preview.output,
            "output_truncated": preview.output_truncated,
            "error": preview.error,
        }

    def restore_view(self, record: RestoreRecord) -> RestoreView:
        status = RestoreStatus(record.status)
        is_active = status in _ACTIVE_STATUSES
        return {
            "run_id": record.restore_id,
            "job": record.request.job,
            "backup": record.request.backup,
            "snapshot_id": record.request.snapshot_id,
            "mode": record.request.mode.value,
            "snapshot_paths": list(record.request.snapshot_paths),
            "include_patterns": list(record.request.include_patterns),
            "exclude_patterns": list(record.request.exclude_patterns),
            "restore_target": str(record.request.restore_target),
            "overwrite": record.request.overwrite,
            "dry_run": record.dry_run,
            "status": status.value,
            "status_label": _STATUS_LABELS[status],
            "status_tone": _STATUS_TONES[status],
            "is_active": is_active,
            "is_cancellable": is_active and record.cancellable,
            "detail_url": f"/runs/{record.restore_id}",
            "status_url": f"/restore/{record.restore_id}/status",
            "cancel_url": f"/restore/{record.restore_id}/cancel",
            "error": record.error,
            "output": record.output,
            "output_truncated": record.output_truncated,
        }

    def _request(
        self,
        config: ResolvedAppConfig,
        job_name: str,
        backup_name: str,
        snapshot_id: str,
        mode: str | RestoreMode,
        snapshot_paths: list[str] | tuple[str, ...] | None,
        include_patterns: list[str] | tuple[str, ...] | None,
        exclude_patterns: list[str] | tuple[str, ...] | None,
        target: str | Path | None,
        overwrite: bool,
        *,
        resolved_backup: ResolvedBackupConfig | None = None,
    ) -> tuple[RestoreRequest, ResolvedBackupConfig]:
        job_name = _validate_name(job_name, "job")
        backup_name = _validate_name(backup_name, "backup")
        snapshot_id = _validate_snapshot_id(snapshot_id)
        backup: ResolvedBackupConfig | None
        if resolved_backup is not None:
            backup = resolved_backup
        else:
            job = get_job_or_raise(config, job_name)
            backup = job.backup.get(backup_name)
            if backup is None:
                raise NotFoundServiceError(f"Backup not found: {job_name}.{backup_name}")
        restore_mode = _validate_restore_mode(mode)
        paths, includes, excludes = _validate_restore_selection(
            restore_mode, snapshot_paths, include_patterns, exclude_patterns
        )
        target_path = (
            _validate_restore_target(target, self._restore_base_dir)
            if target is not None
            else _default_restore_target(
                self._restore_base_dir,
                job_name,
                backup_name,
                snapshot_id,
                datetime.now(timezone.utc),
            )
        )
        _validate_restore_target(target_path, self._restore_base_dir)
        return (
            RestoreRequest(
                job=job_name,
                backup=backup_name,
                snapshot_id=snapshot_id,
                mode=restore_mode,
                snapshot_paths=paths,
                include_patterns=includes,
                exclude_patterns=excludes,
                restore_target=target_path,
                overwrite=overwrite,
            ),
            backup,
        )

    async def _sync_run_record(
        self, record: RestoreRecord, run_record: RunRecord | None = None
    ) -> None:
        if record.status not in _ACTIVE_STATUSES:
            self._pending_restores.pop(record.restore_id, None)
            return
        if run_record is None:
            try:
                run_record = await self._run_manager.get(record.restore_id)
            except NotFoundServiceError:
                if record.restore_id in self._pending_restores:
                    return
                if record.status in _ACTIVE_STATUSES:
                    status = RestoreStatus.UNEXPECTED_ERROR
                    error = record.error or "Restore runtime is no longer active"
                    finished_at = record.finished_at or datetime.now(timezone.utc)
                    record.status = status
                    record.error = error
                    record.finished_at = finished_at
                    await self._registry.update(
                        record.restore_id,
                        status=status,
                        error=error,
                        finished_at=finished_at,
                    )
                return
        status = _restore_status_for_run(run_record.status)
        changes: dict[str, object] = {}
        if record.status in _ACTIVE_STATUSES:
            if (
                record.restore_id in self._pending_restores
                and record.status == RestoreStatus.QUEUED
                and status == RestoreStatus.RUNNING
                and await self._registry.get(record.restore_id) is None
            ):
                return
            changes["status"] = status
            changes["cancellable"] = run_record.cancellable
            changes["started_at"] = run_record.started_at
            changes["finished_at"] = run_record.finished_at
            if status == RestoreStatus.CANCELLED:
                changes["error"] = "Restore task was cancelled"
            elif run_record.error is not None:
                changes["error"] = run_record.error
        if changes:
            if record.restore_id in self._pending_restores:
                for key, value in changes.items():
                    setattr(record, key, value)
                if status not in _ACTIVE_STATUSES:
                    self._pending_restores.pop(record.restore_id, None)
                    await asyncio.sleep(0)
            else:
                for key, value in changes.items():
                    setattr(record, key, value)
                if status not in _ACTIVE_STATUSES:
                    await self._registry.update(record.restore_id, **changes)
                    await asyncio.sleep(0)

    async def _execute(
        self,
        restore_id: str,
        request: RestoreRequest,
        backup: ResolvedBackupConfig,
        log_level: str,
        dry_run: bool,
        on_operational_complete: Callable[[], object],
    ) -> bool:
        with log_task_context(f"restore.{request.backup}"):
            return await self._execute_tagged(
                restore_id, request, backup, log_level, dry_run, on_operational_complete
            )

    async def _execute_tagged(
        self,
        restore_id: str,
        request: RestoreRequest,
        backup: ResolvedBackupConfig,
        log_level: str,
        dry_run: bool,
        on_operational_complete: Callable[[], object],
    ) -> bool:
        try:
            command = _build_restore_command(backup, request, dry_run=dry_run)
            result = await _run_locked_restore(
                command=command,
                backup=backup,
                request=request,
                registry=self._registry,
                restore_id=restore_id,
                dry_run=dry_run,
                log_level=log_level,
                lock_dir=self._lock_dir,
                log_base_dir=self._log_base_dir,
                timeout=backup.timeouts.backup_timeout,
                restore_base_dir=self._restore_base_dir,
            )
            self._mark_operational_complete(on_operational_complete)
            await self._registry.update(
                restore_id,
                error=result.error,
                output=result.output,
                output_truncated=result.output_truncated,
            )
            return result.ok
        except asyncio.CancelledError:
            await _update_restore_resisting_cancellation(
                self._registry,
                restore_id,
                error="Restore task was cancelled",
            )
            raise
        except JobAlreadyRunningError as exc:
            await self._registry.update(
                restore_id,
                status=RestoreStatus.LOCK_ERROR,
                error=str(exc),
                finished_at=datetime.now(timezone.utc),
            )
            raise
        except ConfigServiceError as exc:
            await self._registry.update(
                restore_id,
                status=RestoreStatus.CONFIG_ERROR,
                error=str(exc),
                finished_at=datetime.now(timezone.utc),
            )
            raise
        except Exception as exc:
            await self._registry.update(
                restore_id,
                status=RestoreStatus.UNEXPECTED_ERROR,
                error=str(exc),
                finished_at=datetime.now(timezone.utc),
            )
            raise
        finally:
            self._pending_restores.pop(restore_id, None)

    def _mark_operational_complete(self, on_operational_complete: Callable[[], object]) -> None:
        try:
            on_operational_complete()
        except Exception:
            logger.warning("Restore operational completion callback failed", exc_info=True)


@dataclass(frozen=True)
class _RestoreRunResult:
    ok: bool
    output: str
    output_truncated: bool
    error: str | None = None


def _build_restore_command(
    backup: ResolvedBackupConfig, request: RestoreRequest, *, dry_run: bool
) -> RestoreCommand:
    argv = [
        "restic",
        "--repo",
        backup.repository,
        "restore",
        request.snapshot_id,
        "--target",
        str(request.restore_target),
        "--overwrite",
        "always" if request.overwrite else "never",
    ]
    if dry_run:
        argv.append("--dry-run")
        argv.append("--verbose=2")
    if request.mode == RestoreMode.BROWSER:
        for snapshot_path in request.snapshot_paths:
            if snapshot_path != "/":
                argv.extend(["--include", snapshot_path])
    else:
        for pattern in request.include_patterns:
            argv.extend(["--include", pattern])
        for pattern in request.exclude_patterns:
            argv.extend(["--exclude", pattern])
    return RestoreCommand(
        argv=argv,
        restore_target=request.restore_target,
        mode=request.mode,
        snapshot_paths=request.snapshot_paths,
        include_patterns=request.include_patterns,
        exclude_patterns=request.exclude_patterns,
        overwrite=request.overwrite,
        dry_run=dry_run,
    )


async def _run_locked_restore(
    command: RestoreCommand,
    backup: ResolvedBackupConfig,
    request: RestoreRequest,
    registry: RestoreRegistry,
    restore_id: str,
    dry_run: bool,
    log_level: str,
    lock_dir: Path,
    log_base_dir: Path,
    timeout: int | None,
    restore_base_dir: Path,
) -> _RestoreRunResult:
    resources = {
        resource_for_repository(backup.repository),
        resource_for_repository(str(command.restore_target)),
    }
    lock_manager = ResourceLockManager(
        request.job, f"restore.{request.backup}", resources, lock_dir=lock_dir
    )
    with lock_manager.acquire():
        await registry.update(
            restore_id,
            status=RestoreStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
        )
        return await _run_restore_command(
            command=command,
            backup=backup,
            job_name=request.job,
            log_level=log_level,
            log_base_dir=log_base_dir,
            timeout=timeout,
            restore_base_dir=restore_base_dir,
        )


async def _run_restore_command(
    *,
    command: RestoreCommand,
    backup: ResolvedBackupConfig,
    job_name: str,
    log_level: str,
    log_base_dir: Path,
    timeout: int | None,
    restore_base_dir: Path,
) -> _RestoreRunResult:
    job_logger = setup_job_logger(job_name, log_level=log_level, log_base_dir=log_base_dir)
    job_logger.info(
        "── RESTORE: %s ──────────────────────────────────────────────",
        command.argv[4],
    )
    job_logger.info("Restore target: %s", command.restore_target)
    job_logger.info("Restore command: %s", _shell_join(command.argv))
    stdout_logger = LineLogBuffer(lambda line: job_logger.info("[restic] %s", line))
    stdout_tail = ByteTailBuffer(RESTORE_TAIL_BYTES)
    stderr_tail = ByteTailBuffer(RESTORE_TAIL_BYTES)

    def _capture_stdout(chunk: bytes) -> None:
        stdout_logger.feed(chunk)
        stdout_tail.feed(chunk)

    try:
        if not command.dry_run:
            command = _prepare_restore_target(command, restore_base_dir)
        result = await stream_command(
            command.argv,
            on_stdout=_capture_stdout,
            on_stderr=stderr_tail.feed,
            env=resolve_env(backup.credentials.password, backup.credentials.password_file),
            timeout=timeout,
            capture_tail_bytes=RESTORE_TAIL_BYTES,
        )
    except asyncio.TimeoutError:
        _log_restore_stderr_tail(job_logger, stderr_tail, logging.ERROR)
        output, truncated = _restore_tail_output(stdout_tail, stderr_tail)
        job_logger.error("restic restore timed out")
        message = f"restic restore timed out: {output or 'no output'}"
        return _RestoreRunResult(False, output, truncated, message)
    except asyncio.CancelledError:
        _log_restore_stderr_tail(job_logger, stderr_tail, logging.ERROR)
        raise
    except OSError as exc:
        output, truncated = limit_output(str(exc))
        job_logger.error("restic restore failed: %s", output)
        return _RestoreRunResult(False, output, truncated, f"restic restore failed: {output}")
    except ServiceError as exc:
        output, truncated = limit_output(str(exc))
        job_logger.error("restic restore target rejected: %s", output)
        return _RestoreRunResult(False, output, truncated, output)
    finally:
        stdout_logger.flush()

    _log_restore_stderr(job_logger, result.stderr, result.returncode)
    raw_output = (result.stdout + result.stderr).strip()
    output, limit_truncated = limit_output(raw_output)
    truncated = limit_truncated or result.stdout_truncated or result.stderr_truncated
    if result.returncode == 0:
        job_logger.info("── RESTORE COMPLETED")
        return _RestoreRunResult(True, output, truncated)
    job_logger.error("restic restore exited with %d", result.returncode)
    message = f"restic restore exited with {result.returncode}: {output or 'no output'}"
    return _RestoreRunResult(False, output, truncated, message)


def _prepare_restore_target(command: RestoreCommand, restore_base_dir: Path) -> RestoreCommand:
    _reject_symlink_restore_target_parts(command.restore_target, restore_base_dir)
    command.restore_target.mkdir(parents=True, exist_ok=True)
    _reject_symlink_restore_target_parts(command.restore_target, restore_base_dir)
    safe_target = _validate_restore_target(command.restore_target, restore_base_dir)
    argv = list(command.argv)
    target_index = argv.index("--target") + 1
    argv[target_index] = str(safe_target)
    return replace(command, argv=argv, restore_target=safe_target)


def _reject_symlink_restore_target_parts(path: Path, restore_base_dir: Path) -> None:
    resolved_base = restore_base_dir.resolve(strict=False)
    for candidate in (path, *path.parents):
        if candidate == resolved_base.parent:
            break
        if candidate.is_symlink():
            raise ServiceError("invalid_restore_target", "Restore target must not use symlinks")


def _validate_name(value: str, kind: str) -> str:
    if not _NAME_RE.fullmatch(value):
        raise ServiceError("invalid_parameter", f"Invalid {kind} name: {value!r}")
    return value


def _validate_snapshot_id(value: str) -> str:
    if not _SNAPSHOT_ID_RE.fullmatch(value):
        raise ServiceError(
            "invalid_parameter",
            "Invalid snapshot id: expected 8 to 64 hexadecimal characters",
        )
    return value


def _validate_restore_mode(value: str | RestoreMode) -> RestoreMode:
    try:
        return RestoreMode(value)
    except ValueError as exc:
        raise ServiceError("invalid_parameter", f"Invalid restore mode: {value!r}") from exc


def _validate_restore_selection(
    mode: RestoreMode,
    snapshot_paths: list[str] | tuple[str, ...] | None,
    include_patterns: list[str] | tuple[str, ...] | None,
    exclude_patterns: list[str] | tuple[str, ...] | None,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    paths = _validate_snapshot_paths(snapshot_paths, default_whole_snapshot=False)
    includes = _validate_patterns(include_patterns)
    excludes = _validate_patterns(exclude_patterns)
    if includes and excludes:
        raise ServiceError("invalid_parameter", "Include and exclude patterns cannot be combined")
    if mode == RestoreMode.PATTERN:
        if paths:
            raise ServiceError(
                "invalid_parameter", "Snapshot paths are only allowed in browser mode"
            )
        return (), includes, excludes
    if includes or excludes:
        raise ServiceError("invalid_parameter", "Patterns are only allowed in pattern mode")
    return paths or ("/",), (), ()


def _validate_patterns(values: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    patterns: list[str] = []
    for value in values or ():
        if "\x00" in value or "\r" in value or "\n" in value:
            raise ServiceError("invalid_parameter", "Invalid restore pattern")
        pattern = value.strip()
        if pattern and pattern not in patterns:
            patterns.append(pattern)
    return tuple(patterns)


def _validate_snapshot_paths(
    values: list[str] | tuple[str, ...] | None, *, default_whole_snapshot: bool = True
) -> tuple[str, ...]:
    if values is None or len(values) == 0:
        return ("/",) if default_whole_snapshot else ()
    paths: list[str] = []
    for value in values:
        path = _validate_snapshot_path(value)
        if path not in paths:
            paths.append(path)
    return tuple(paths)


def _validate_snapshot_path(value: str) -> str:
    if any(char in value for char in ("\x00", "\\", "\r", "\n")):
        raise ServiceError("invalid_parameter", "Invalid snapshot path")
    if not value.startswith("/"):
        raise ServiceError("invalid_parameter", "Snapshot path must be absolute")
    normalized = value.rstrip("/") or "/"
    if normalized == "/":
        return normalized
    if _RESTIC_GLOB_META_RE.search(normalized):
        raise ServiceError(
            "invalid_parameter",
            "Browser restore paths must not contain glob characters (*, ?, [)",
        )
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts[1:]):
        raise ServiceError("invalid_parameter", "Invalid snapshot path")
    return normalized


def _resolve_restore_base(value: Path) -> Path:
    expanded = value.expanduser()
    if not expanded.is_absolute():
        raise ServiceError("invalid_restore_base", "Restore base must be absolute")
    return expanded.resolve(strict=False)


def _default_restore_base_dir() -> Path:
    return Path(os.environ.get("DK_RESTORE_DIR", str(DEFAULT_RESTORE_BASE_DIR)))


def _validate_restore_target(value: str | Path, restore_base_dir: Path) -> Path:
    raw_value = str(value)
    if "\x00" in raw_value or "\\" in raw_value:
        raise ServiceError("invalid_restore_target", "Invalid restore target")
    if not raw_value.startswith("/"):
        raise ServiceError("invalid_restore_target", "Restore target must be absolute")
    raw_parts = raw_value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts[1:]):
        raise ServiceError("invalid_restore_target", "Invalid restore target")
    path = Path(raw_value)
    resolved = path.expanduser().resolve(strict=False)
    if resolved != restore_base_dir and restore_base_dir not in resolved.parents:
        raise ServiceError("invalid_restore_target", "Restore target must be under restore base")
    return resolved


def _default_restore_target(
    restore_base_dir: Path,
    job_name: str,
    backup_name: str,
    snapshot_id: str,
    timestamp: datetime,
) -> Path:
    stamp = timestamp.astimezone(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    leaf = f"{stamp}_{snapshot_id[:8]}_{uuid4().hex[:8]}"
    return restore_base_dir / job_name / backup_name / leaf


def _restore_record_to_row(record: RestoreRecord) -> tuple[
    str,
    str,
    str,
    str,
    str,
    str,
    str,
    str,
    str,
    str,
    str,
    int,
    str | None,
    str | None,
    int,
]:
    request = record.request
    return (
        str(uuid4()),
        record.restore_id,
        request.job,
        request.backup,
        "restic",
        request.snapshot_id,
        request.mode.value,
        str(request.restore_target),
        _json_list(request.snapshot_paths),
        _json_list(request.include_patterns),
        _json_list(request.exclude_patterns),
        int(request.overwrite),
        record.error,
        record.output,
        int(record.output_truncated),
    )


def _restore_row_to_record(row: sqlite3.Row) -> RestoreRecord:
    status = RestoreStatus(str(row["status"]))
    error = row["error"] or row["run_error"]
    if status == RestoreStatus.CANCELLED and error is None:
        error = "Restore task was cancelled"
    finished_at = _dt_from_text(row["finished_at"])
    return RestoreRecord(
        restore_id=str(row["restore_id"]),
        request=RestoreRequest(
            job=str(row["job"]),
            backup=str(row["backup"]),
            snapshot_id=str(row["snapshot_id"]),
            mode=RestoreMode(str(row["mode"])),
            restore_target=Path(str(row["restore_target"])),
            snapshot_paths=tuple(_json_string_list(row["snapshot_paths_json"])),
            include_patterns=tuple(_json_string_list(row["include_patterns_json"])),
            exclude_patterns=tuple(_json_string_list(row["exclude_patterns_json"])),
            overwrite=bool(row["overwrite"]),
        ),
        status=status,
        dry_run=bool(row["dry_run"]),
        started_at=_dt_from_text(row["started_at"]),
        finished_at=finished_at,
        error=str(error) if error is not None else None,
        output=str(row["output"]) if row["output"] is not None else None,
        output_truncated=bool(row["output_truncated"]),
        created_at=_dt_from_text(row["started_at"]) or datetime.now(timezone.utc),
    )


def _json_list(values: tuple[str, ...]) -> str:
    return json.dumps(list(values), separators=(",", ":"))


def _json_string_list(value: object) -> list[str]:
    raw = json.loads(str(value))
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError("Expected JSON string list")
    return raw


def _dt_to_text(value: datetime | None) -> str | None:
    return utc_rfc3339(value) if value is not None else None


def _dt_from_text(value: object) -> datetime | None:
    return parse_appdata_datetime(value)


async def _update_restore_resisting_cancellation(
    registry: RestoreRegistry, restore_id: str, **changes: object
) -> None:
    update_task = asyncio.create_task(registry.update(restore_id, **changes))
    cancellation: asyncio.CancelledError | None = None
    while not update_task.done():
        try:
            await asyncio.shield(update_task)
        except asyncio.CancelledError as exc:
            cancellation = exc
    try:
        update_task.result()
    except KeyError:
        if cancellation is not None:
            raise cancellation
        return
    if cancellation is not None:
        raise cancellation


def _restore_status_for_run(status: RunStatus) -> RestoreStatus:
    if status == RunStatus.SKIPPED:
        return RestoreStatus.LOCK_ERROR
    return RestoreStatus(status.value)


def _log_restore_stderr(job_logger: logging.Logger, stderr: str, returncode: int) -> None:
    """Log the bounded restic stderr tail once, level chosen by exit code.

    Each line is its own log record so the GUI level filter can isolate it.
    """
    if not stderr:
        return
    level = logging.DEBUG if returncode == 0 else logging.ERROR
    for line in stderr.splitlines():
        job_logger.log(level, "[restic] %s", line)


def _log_restore_stderr_tail(
    job_logger: logging.Logger, stderr_tail: ByteTailBuffer, level: int
) -> None:
    for line in stderr_tail.decode().splitlines():
        job_logger.log(level, "[restic] %s", line)


def _restore_tail_output(
    stdout_tail: ByteTailBuffer, stderr_tail: ByteTailBuffer
) -> tuple[str, bool]:
    raw_output = (stdout_tail.decode() + stderr_tail.decode()).strip()
    output, limit_truncated = limit_output(raw_output)
    return output, limit_truncated or stdout_tail.truncated or stderr_tail.truncated


def _shell_join(argv: list[str]) -> str:
    return " ".join(_quote_arg(arg) for arg in argv)


def _quote_arg(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:=+-]+", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"
