"""Restic repository diagnostics for the GUI."""

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, cast

from ..core.subprocesses import run_command
from ..models.resolved_config import ResolvedBackupConfig
from ..utils.restic import resolve_env
from ..utils.timeouts import env_timeout
from .async_bridge import run_sync
from .config import ConfigService, get_job_or_raise
from .errors import NotFoundServiceError, ServiceError
from .output import limit_output
from .repository_artifact_store import RepositoryArtifactStore, repository_location_key_for_display

logger = logging.getLogger(__name__)

STATS_TIMEOUT_ENV = "DK_STATS_TIMEOUT"
DEFAULT_STATS_TIMEOUT = 600
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class ResticJsonResult:
    """Internal result for restic JSON commands."""

    ok: bool
    data: Any
    error: str | None = None


class RepositoryService:

    def __init__(
        self,
        config_service: ConfigService,
        backup_artifact_store: RepositoryArtifactStore | None = None,
    ) -> None:
        self._config_service = config_service
        self._backup_artifact_store = backup_artifact_store

    async def snapshots_async(
        self,
        job_name: str,
        backup_name: str | None = None,
        *,
        latest: int | None = None,
        timeout: int | None = None,
        timeout_env_hint: str | None = None,
    ) -> dict[str, object]:
        backup_name, backup = self._backup(job_name, backup_name)
        cmd = ["restic", "--repo", backup.repository, "snapshots"]
        if latest is not None:
            cmd.extend(["--latest", str(latest)])
        cmd.append("--json")
        result = await _run_restic_json(
            cmd,
            backup,
            _stats_timeout(timeout),
            timeout_env_hint=timeout_env_hint or (STATS_TIMEOUT_ENV if timeout is None else None),
        )
        if result.ok and not isinstance(result.data, list):
            return {
                "job": job_name,
                "backup": backup_name,
                "repository": backup.repository,
                "snapshots": [],
                "error": "unexpected restic snapshots response",
            }
        snapshots = result.data if result.ok else []
        return {
            "job": job_name,
            "backup": backup_name,
            "repository": backup.repository,
            "snapshots": snapshots,
            "error": result.error,
        }

    def snapshots(
        self,
        job_name: str,
        backup_name: str | None = None,
        *,
        latest: int | None = None,
        timeout: int | None = None,
        timeout_env_hint: str | None = None,
    ) -> dict[str, object]:
        return run_sync(
            self.snapshots_async(
                job_name,
                backup_name,
                latest=latest,
                timeout=timeout,
                timeout_env_hint=timeout_env_hint,
            )
        )

    async def stats_async(
        self,
        job_name: str,
        backup_name: str | None = None,
        *,
        mode: str | None = None,
        timeout: int | None = None,
        timeout_env_hint: str | None = None,
    ) -> dict[str, object]:
        backup_name, backup = self._backup(job_name, backup_name)
        cmd = ["restic", "--repo", backup.repository, "stats", "--json"]
        if mode:
            cmd.extend(["--mode", mode])
        result = await _run_restic_json(
            cmd,
            backup,
            _stats_timeout(timeout),
            timeout_env_hint=timeout_env_hint or (STATS_TIMEOUT_ENV if timeout is None else None),
        )
        if not result.ok:
            return {
                "job": job_name,
                "backup": backup_name,
                "repository": backup.repository,
                "available": False,
                "stats": {},
                "error": result.error,
            }
        if not isinstance(result.data, dict):
            return {
                "job": job_name,
                "backup": backup_name,
                "repository": backup.repository,
                "available": False,
                "stats": {},
                "error": "unexpected restic stats response",
            }
        stats = result.data
        return {
            "job": job_name,
            "backup": backup_name,
            "repository": backup.repository,
            "available": bool(stats),
            "stats": stats,
            "error": None if stats else "empty restic stats response",
        }

    def stats(
        self,
        job_name: str,
        backup_name: str | None = None,
        *,
        mode: str | None = None,
        timeout: int | None = None,
        timeout_env_hint: str | None = None,
    ) -> dict[str, object]:
        return run_sync(
            self.stats_async(
                job_name,
                backup_name,
                mode=mode,
                timeout=timeout,
                timeout_env_hint=timeout_env_hint,
            )
        )

    async def backend_repository_id_async(
        self,
        job_name: str,
        backup_name: str | None = None,
        *,
        timeout: int | None = None,
        timeout_env_hint: str | None = None,
    ) -> str | None:
        _, backup = self._backup(job_name, backup_name)
        result = await _run_restic_json(
            ["restic", "--repo", backup.repository, "cat", "config", "--json"],
            backup,
            _stats_timeout(timeout),
            timeout_env_hint=timeout_env_hint or (STATS_TIMEOUT_ENV if timeout is None else None),
        )
        if not result.ok or not isinstance(result.data, dict):
            return None
        repository_id = result.data.get("id")
        if not isinstance(repository_id, str) or not repository_id:
            return None
        return repository_id

    def backend_repository_id(
        self,
        job_name: str,
        backup_name: str | None = None,
        *,
        timeout: int | None = None,
        timeout_env_hint: str | None = None,
    ) -> str | None:
        return run_sync(
            self.backend_repository_id_async(
                job_name,
                backup_name,
                timeout=timeout,
                timeout_env_hint=timeout_env_hint,
            )
        )

    def ensure_backup(self, job_name: str, backup_name: str) -> None:
        """Raise a structured error if the job/backup target does not exist.

        Mirrors the not-found behaviour of the repository GET routes so callers
        (e.g. the snapshot refresh adapter) can translate an unknown target into
        a 404 instead of silently succeeding.
        """
        self._backup(job_name, backup_name)

    def delete_location(self, repository_id: str, location_id: str) -> None:
        """Hard-delete a stored repository location."""
        if self._backup_artifact_store is None:
            raise ServiceError(
                "artifact_store_unavailable",
                "Backup artifact store is not available",
                500,
            )
        repository_id = repository_id.strip()
        location_id = location_id.strip()
        if not repository_id or not location_id:
            raise ServiceError("invalid_parameter", "Repository and location IDs are required", 400)
        if not self._backup_artifact_store.delete_location(repository_id, location_id):
            raise NotFoundServiceError("Repository location not found")

    def merge_location(
        self, repository_id: str, source_location_id: str, target_location_id: str
    ) -> None:
        """Merge one stored repository location into another."""
        if self._backup_artifact_store is None:
            raise ServiceError(
                "artifact_store_unavailable",
                "Backup artifact store is not available",
                500,
            )
        repository_id = repository_id.strip()
        source_location_id = source_location_id.strip()
        target_location_id = target_location_id.strip()
        if not repository_id or not source_location_id or not target_location_id:
            raise ServiceError(
                "invalid_parameter",
                "Repository, source location and target location IDs are required",
                400,
            )
        if source_location_id == target_location_id:
            raise ServiceError("invalid_parameter", "Merge target must differ from source", 400)
        if not self._backup_artifact_store.merge_location(
            repository_id, source_location_id, target_location_id
        ):
            raise NotFoundServiceError("Repository location not found")

    def configured_repository_location_keys(self) -> frozenset[str]:
        """Return canonical location keys for every job.backup in the active config.

        Used to protect repository locations that are still referenced by the
        config from count-based retention, regardless of how old they are.
        """
        config = self._config_service.load_active_config()
        return frozenset(
            repository_location_key_for_display(backup.repository)
            for job in config.jobs.values()
            for backup in job.backup.values()
        )

    def _backup(self, job_name: str, backup_name: str | None) -> tuple[str, ResolvedBackupConfig]:
        _validate_name(job_name, "job")
        if backup_name is not None:
            _validate_name(backup_name, "backup")
        config = self._config_service.load_active_config()
        job = get_job_or_raise(config, job_name)
        if backup_name is None:
            backup_name = next(iter(job.backup), None)
        if backup_name is None:
            raise NotFoundServiceError(f"No backup configured for job: {job_name}")
        backup = job.backup.get(backup_name)
        if backup is None:
            raise NotFoundServiceError(f"Backup not found: {job_name}.{backup_name}")
        return backup_name, backup


def _validate_name(value: str, kind: str) -> str:
    if not _NAME_RE.fullmatch(value):
        raise ServiceError("invalid_parameter", f"Invalid {kind} name: {value!r}", 400)
    return value


def _stats_timeout(timeout: int | None) -> int:
    if timeout is not None:
        return timeout
    return env_timeout(STATS_TIMEOUT_ENV, DEFAULT_STATS_TIMEOUT, logger)


async def _run_restic_json(
    cmd: list[str],
    backup: ResolvedBackupConfig,
    timeout: int,
    *,
    timeout_env_hint: str | None = None,
) -> ResticJsonResult:
    try:
        result = await run_command(
            cmd,
            timeout=timeout,
            env=resolve_env(backup.credentials.password, backup.credentials.password_file),
        )
    except asyncio.TimeoutError as exc:
        output, _ = limit_output(str(exc))
        hint = f" Adjust {timeout_env_hint} to change this timeout." if timeout_env_hint else ""
        return ResticJsonResult(False, None, f"restic command timed out: {output}.{hint}")
    except OSError as exc:
        output, _ = limit_output(str(exc))
        return ResticJsonResult(False, None, f"restic command failed: {output}")
    if result.returncode != 0:
        raw_output = (result.stderr + result.stdout).strip()
        output, _ = limit_output(raw_output)
        return ResticJsonResult(
            False,
            None,
            f"restic exited with {result.returncode}: {output or 'no output'}",
        )
    try:
        return ResticJsonResult(True, cast(Any, json.loads(result.stdout)))
    except json.JSONDecodeError as exc:
        output, _ = limit_output(str(exc))
        return ResticJsonResult(False, None, f"invalid restic JSON output: {output}")
