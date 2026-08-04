"""Collect restic stats for a backup and persist v3 backup artifact data."""

import asyncio
import logging
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TypeVar

from ..core.job_runner import BackupRunStatsContext
from ..utils.timeouts import env_timeout
from .async_bridge import run_sync
from .errors import ConfigServiceError
from .repositories import DEFAULT_STATS_TIMEOUT, STATS_TIMEOUT_ENV, RepositoryService
from .repository_artifact_store import (
    REPOSITORY_IDENTITY_CHANGED,
    RepositoryArtifactStore,
    repository_location_key_for_display,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


class BackupStatsCollector:
    """Collects restic stats for a backup and writes them to the store.

    Restic errors are logged and swallowed; the operational path awaits this
    collector after a successful backup step while the resource lock is held.
    """

    def __init__(
        self,
        repository_service: RepositoryService,
        store: RepositoryArtifactStore,
        timeout: int | None = None,
    ) -> None:
        self._repo_service = repository_service
        self._store = store
        self._timeout = (
            timeout
            if timeout is not None
            else env_timeout(STATS_TIMEOUT_ENV, DEFAULT_STATS_TIMEOUT, logger)
        )
        self._backup_locks: dict[tuple[str, str], threading.Lock] = {}
        self._backup_locks_lock = threading.Lock()

    def collect_and_store(
        self,
        job: str,
        backup: str,
        context: BackupRunStatsContext | None = None,
    ) -> dict[str, object] | None:
        """Run restic stats + snapshots, build the stats dict, and write it.

        Args:
            job: Job name.
            backup: Backup name.

        Returns:
            The written stats dict, or None on total failure.
        """
        return run_sync(self.collect_and_store_async(job, backup, context))

    async def collect_and_store_async(
        self,
        job: str,
        backup: str,
        context: BackupRunStatsContext | None = None,
    ) -> dict[str, object] | None:
        try:
            return await self._collect(job, backup, context=context)
        except Exception:
            logger.warning("Stats collection failed for %s.%s", job, backup, exc_info=True)
            return None

    async def _collect(
        self,
        job: str,
        backup: str,
        *,
        context: BackupRunStatsContext | None,
    ) -> dict[str, object] | None:
        trigger_kind = context.trigger_kind if context is not None else "refresh"
        if trigger_kind not in {"backup", "retention", "cleanup", "refresh"}:
            logger.warning(
                "Unsupported stats trigger %r for %s.%s; skipping write",
                trigger_kind,
                job,
                backup,
            )
            return None

        backend_repository_id = await self._repo_service.backend_repository_id_async(
            job,
            backup,
            timeout=self._timeout,
            timeout_env_hint=STATS_TIMEOUT_ENV,
        )
        if backend_repository_id is None:
            logger.warning(
                "restic cat config did not return a repository id for %s.%s; "
                "skipping artifact persistence",
                job,
                backup,
            )
            return None

        collect_stats = trigger_kind in {"backup", "cleanup", "refresh"}
        collect_snapshots = trigger_kind in {"backup", "retention", "refresh"}
        latest_limit = 1 if trigger_kind == "backup" else None

        stats_payload: dict[str, object] | None = None
        repository = ""
        if collect_stats:
            stats_result = await self._repo_service.stats_async(
                job,
                backup,
                mode="raw-data",
                timeout=self._timeout,
                timeout_env_hint=STATS_TIMEOUT_ENV,
            )
            stats_payload = self._stats_payload(job, backup, stats_result)
            if isinstance(stats_result, dict):
                repository = str(stats_result.get("repository", "") or "")

        snap_result: dict[str, object] | None = None
        if collect_snapshots:
            snap_result = await self._repo_service.snapshots_async(
                job,
                backup,
                latest=latest_limit,
                timeout=self._timeout,
                timeout_env_hint=STATS_TIMEOUT_ENV,
            )
        raw_snaps: list[object] = []
        snapshots_query_ok = not collect_snapshots

        if isinstance(snap_result, dict):
            if not repository:
                repository = str(snap_result.get("repository", "") or "")
            snapshots_query_ok = not snap_result.get("error")
            if snap_result.get("error"):
                logger.warning(
                    "restic snapshots unavailable for %s.%s: %s",
                    job,
                    backup,
                    snap_result["error"],
                )
            snaps_val = snap_result.get("snapshots", [])
            raw_snaps = snaps_val if isinstance(snaps_val, list) else []

        if collect_snapshots and not snapshots_query_ok and trigger_kind in {"retention"}:
            if not repository:
                return None
            return await self._store_call(
                self._read_backup_view_locked,
                job,
                backup,
                repository_location_key_for_display(repository),
            )

        enriched_snaps = [snap for snap in raw_snaps if isinstance(snap, dict)]

        if (
            stats_payload is None
            and not enriched_snaps
            and not (trigger_kind == "backup" and snapshots_query_ok)
        ):
            logger.warning("No usable data collected for %s.%s — skipping write", job, backup)
            return None

        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if not repository and isinstance(snap_result, dict):
            repository = str(snap_result.get("repository", ""))
        if not repository:
            logger.warning(
                "No repository location collected for %s.%s — skipping write",
                job,
                backup,
            )
            return None
        location_key = repository_location_key_for_display(repository)

        location_resolution = await self._store_call(
            self._resolve_observed_location_locked,
            job,
            backup,
            backend_repository_id=backend_repository_id,
            repository_location_key=location_key,
            display_repository=repository,
            observed_at=timestamp,
        )
        identity_changed = location_resolution.get("status") == REPOSITORY_IDENTITY_CHANGED

        if identity_changed and latest_limit is not None:
            snap_result = await self._repo_service.snapshots_async(
                job,
                backup,
                latest=None,
                timeout=self._timeout,
                timeout_env_hint=STATS_TIMEOUT_ENV,
            )
            snapshots_query_ok = isinstance(snap_result, dict) and not snap_result.get("error")
            raw_snaps = []
            if isinstance(snap_result, dict):
                refreshed_snaps = snap_result.get("snapshots", [])
                if isinstance(refreshed_snaps, list):
                    raw_snaps = refreshed_snaps
            enriched_snaps = [snap for snap in raw_snaps if isinstance(snap, dict)]

        protected_location_keys: frozenset[str] | None = frozenset({location_key})
        retention_due = await self._store_call(
            self._store.is_retention_due,
            str(location_resolution["repository_id"]),
            at=timestamp,
        )
        if retention_due:
            try:
                protected_location_keys = await self._store_call(
                    self._repo_service.configured_repository_location_keys
                )
            except ConfigServiceError:
                protected_location_keys = None
                logger.warning(
                    "Config unavailable while preparing artifact retention for %s.%s; "
                    "persisting collected data and skipping artifact retention",
                    job,
                    backup,
                    exc_info=True,
                )
        full_reconcile = (
            snapshots_query_ok
            if context is None
            else ((trigger_kind == "retention" or identity_changed) and snapshots_query_ok)
        )
        data = await self._store_call(
            self._persist_and_read_locked,
            job,
            backup,
            repository=repository,
            location_key=location_key,
            backend_repository_id=backend_repository_id,
            artifacts=enriched_snaps if snapshots_query_ok else [],
            context=context,
            stats=stats_payload,
            observed_at=timestamp,
            full_reconcile=full_reconcile,
            protected_location_keys=protected_location_keys,
        )
        logger.debug("Stats written for %s.%s", job, backup)
        return data

    def _stats_payload(
        self,
        job: str,
        backup: str,
        stats_result: dict[str, object] | object,
    ) -> dict[str, object] | None:
        if not isinstance(stats_result, dict):
            stats_result = {}
        stats_data = stats_result.get("stats", {})
        if not (isinstance(stats_data, dict) and stats_result.get("available")):
            logger.warning(
                "restic stats (mode=raw-data) unavailable for %s.%s: %s",
                job,
                backup,
                stats_result.get("error") if isinstance(stats_result, dict) else "unknown",
            )
            return None

        total_size = int(stats_data.get("total_size", 0) or 0)
        snapshots_count = int(stats_data.get("snapshots_count", 0) or 0)
        raw_count = stats_data.get("total_file_count")
        return {
            "mode": "raw-data",
            "total_size_bytes": total_size,
            "total_file_count": int(raw_count) if raw_count is not None else None,
            "snapshots_count": snapshots_count,
            "last_backup": None,
        }

    def _backup_lock(self, job: str, backup: str) -> threading.Lock:
        key = (job, backup)
        with self._backup_locks_lock:
            lock = self._backup_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._backup_locks[key] = lock
            return lock

    def _read_backup_view_locked(
        self, job: str, backup: str, location_key: str
    ) -> dict[str, object] | None:
        with self._backup_lock(job, backup):
            return self._store.read_backup_view_for_location(job, backup, location_key)

    def _resolve_observed_location_locked(
        self,
        job: str,
        backup: str,
        *,
        backend_repository_id: str,
        repository_location_key: str,
        display_repository: str,
        observed_at: str,
    ) -> dict[str, object]:
        with self._backup_lock(job, backup):
            return self._store.resolve_observed_location(
                backend="restic",
                backend_repository_id=backend_repository_id,
                repository_location_key=repository_location_key,
                display_repository=display_repository,
                observed_at=observed_at,
            )

    def _persist_and_read_locked(
        self,
        job: str,
        backup: str,
        *,
        repository: str,
        location_key: str,
        backend_repository_id: str,
        artifacts: list[dict[str, object]],
        context: BackupRunStatsContext | None,
        stats: dict[str, object] | None,
        observed_at: str,
        full_reconcile: bool,
        protected_location_keys: frozenset[str] | None,
    ) -> dict[str, object] | None:
        with self._backup_lock(job, backup):
            if context is None:
                persisted = self._store.persist_refresh(
                    job=job,
                    backup=backup,
                    repository=repository,
                    repository_location_key=location_key,
                    display_repository=repository,
                    backend_repository_id=backend_repository_id,
                    artifacts=artifacts,
                    stats=stats,
                    observed_at=observed_at,
                    full_reconcile=full_reconcile,
                    protected_location_keys=protected_location_keys,
                )
            else:
                persisted = self._store.persist_backup_run(
                    job=job,
                    backup=backup,
                    repository=repository,
                    repository_location_key=location_key,
                    display_repository=repository,
                    backend_repository_id=backend_repository_id,
                    artifacts=artifacts,
                    context=context,
                    stats=stats,
                    observed_at=observed_at,
                    full_reconcile=full_reconcile,
                    protected_location_keys=protected_location_keys,
                )
            if not persisted:
                return None
            return self._store.read_backup_view_for_location(job, backup, location_key)

    async def _store_call(
        self,
        func: Callable[..., T],
        /,
        *args: object,
        **kwargs: object,
    ) -> T:
        task = asyncio.ensure_future(asyncio.to_thread(func, *args, **kwargs))
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancellation = exc
            except Exception:
                break
        result = task.result()
        if cancellation is not None:
            raise cancellation
        return result
