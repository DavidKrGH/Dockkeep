"""SQLite-backed store for repository-centered backup artifacts."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from ..core.job_runner import BackupRunStatsContext
from ..core.locking import canonical_resource_id
from .appdata_schema import connect_appdata_db, immediate_tx, parse_appdata_datetime, utc_rfc3339
from .backup_stats_helpers import compute_duration
from .errors import ServiceError
from .run_history import (
    appdata_retention_count,
    appdata_retention_days,
    default_appdata_db_path,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 3
DEFAULT_LOCATION_LIMIT = 10
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
REPOSITORY_IDENTITY_CHANGED = "repository_identity_changed"
ARTIFACT_RETENTION_META_PREFIX = "repository_artifact_retention_date:"


class _MergeLocationNoOpError(Exception):
    """Signals an early, no-op exit from a ``merge_location`` transaction.

    A plain ``return`` inside ``immediate_tx``'s ``with`` block would commit
    rather than roll back (normal exit takes the commit path); raising this
    keeps the no-op case on the rollback path even though nothing was
    written yet.
    """


def _new_sortable_id() -> str:
    """Return a unique ID whose lexicographic order matches creation order.

    Used only where deletion/retention logic must rank rows by creation order
    (see ``_prune_locations_by_count``) without a separate timestamp column.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    return f"{timestamp}-{uuid4().hex}"


class RepositoryArtifactStore:

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or default_appdata_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._retention_days = appdata_retention_days()
        self._retention_count = appdata_retention_count()

    def persist_backup_run(
        self,
        *,
        job: str,
        backup: str,
        repository: str | None = None,
        repository_location_key: str | None = None,
        display_repository: str | None = None,
        backend_repository_id: str,
        artifacts: list[dict[str, object]] | None = None,
        context: BackupRunStatsContext | None = None,
        stats: dict[str, object] | None = None,
        backend: str = "restic",
        observed_at: str | None = None,
        full_reconcile: bool = False,
        trigger_kind: str = "backup",
        run_step_id: str | None = None,
        protected_location_keys: frozenset[str] | None = frozenset(),
    ) -> bool:
        """Persist one successful backup/maintenance post-processing result.

        Backend calls are deliberately outside this method. Database failures are
        logged and returned as ``None`` so operational runs remain successful.
        ``protected_location_keys`` are canonical repository location keys
        currently referenced by the active config; they are exempt from
        count-based location retention (see ``_prune_locations_by_count``).
        Passing ``None`` skips the daily retention attempt for this write.
        """
        _validate_name(job, "job")
        _validate_name(backup, "backup")
        now = _canonical_time_text(observed_at) or _stats_collected_at(stats) or _now_text()
        display = display_repository or repository or repository_location_key
        location_key = repository_location_key or (
            repository_location_key_for_display(display) if display is not None else None
        )
        if display is None or location_key is None:
            raise ServiceError("invalid_parameter", "Repository location is required", 400)
        run_context = context or BackupRunStatsContext(
            run_id=run_step_id,
            run_step_id=run_step_id,
            backup_summary=None,
            trigger_kind=trigger_kind,
        )
        try:
            with immediate_tx(lambda: self.connect()) as conn:
                result = self._persist_observation(
                    conn,
                    job=job,
                    backup=backup,
                    display_repository=display,
                    repository_location_key=location_key,
                    backend=backend,
                    backend_repository_id=backend_repository_id,
                    artifacts=artifacts or [],
                    stats=stats,
                    trigger_kind=run_context.trigger_kind,
                    run_step_id=run_context.run_step_id,
                    backup_summary=run_context.backup_summary,
                    observed_at=now,
                    full_reconcile=full_reconcile,
                )
                self._apply_retention_if_due(
                    conn,
                    repository_id=str(result["repository_id"]),
                    at=now,
                    protected_location_keys=protected_location_keys,
                )
                return True
        except Exception:
            logger.exception("Failed to persist backup artifact data for %s.%s", job, backup)
            return False

    def persist_refresh(
        self,
        *,
        job: str,
        backup: str,
        repository: str | None = None,
        repository_location_key: str | None = None,
        display_repository: str | None = None,
        backend_repository_id: str,
        artifacts: list[dict[str, object]] | None = None,
        stats: dict[str, object] | None = None,
        backend: str = "restic",
        observed_at: str | None = None,
        full_reconcile: bool = True,
        protected_location_keys: frozenset[str] | None = frozenset(),
    ) -> bool:
        _validate_name(job, "job")
        _validate_name(backup, "backup")
        now = _canonical_time_text(observed_at) or _stats_collected_at(stats) or _now_text()
        display = display_repository or repository or repository_location_key
        location_key = repository_location_key or (
            repository_location_key_for_display(display) if display is not None else None
        )
        if display is None or location_key is None:
            raise ServiceError("invalid_parameter", "Repository location is required", 400)
        try:
            with immediate_tx(lambda: self.connect()) as conn:
                result = self._persist_observation(
                    conn,
                    job=job,
                    backup=backup,
                    display_repository=display,
                    repository_location_key=location_key,
                    backend=backend,
                    backend_repository_id=backend_repository_id,
                    artifacts=artifacts or [],
                    stats=stats,
                    trigger_kind="refresh",
                    run_step_id=None,
                    backup_summary=None,
                    observed_at=now,
                    full_reconcile=full_reconcile,
                )
                self._apply_retention_if_due(
                    conn,
                    repository_id=str(result["repository_id"]),
                    at=now,
                    protected_location_keys=protected_location_keys,
                )
                return True
        except Exception:
            logger.exception("Failed to persist backup artifact refresh for %s.%s", job, backup)
            return False

    def resolve_observed_location(
        self,
        *,
        backend: str,
        backend_repository_id: str,
        repository_location_key: str,
        display_repository: str,
        observed_at: str | None = None,
    ) -> dict[str, object]:
        """Resolve one backend observation without writing artifact data.

        The lookup is keyed by the canonical repository location.  A known
        location is trusted only when the freshly read backend repository ID
        still matches its stored repository mapping.  If the backend identity
        changed, the location is rebound in a short write transaction and the
        caller can trigger a separate full refresh before persisting artifacts.
        """
        now = _canonical_time_text(observed_at) or _now_text()
        with closing(self._connect()) as conn:
            row = _location_by_key(conn, repository_location_key)
            if row is not None and row["backend_repository_id"] == backend_repository_id:
                return {
                    "status": "known_location",
                    "repository_id": row["repository_id"],
                    "location_id": row["location_id"],
                }
            if row is not None:
                old_repository_id = str(row["repository_id"])
                with immediate_tx(lambda: self.connect()) as write_conn:
                    _rebind_location_identity(
                        write_conn,
                        backend=backend,
                        backend_repository_id=backend_repository_id,
                        repository_location_key=repository_location_key,
                        display_repository=display_repository,
                        old_repository_id=old_repository_id,
                        now=now,
                    )
                    rebound = _location_by_key(write_conn, repository_location_key)
                if rebound is None:
                    raise RuntimeError("Location rebind failed")
                return {
                    "status": REPOSITORY_IDENTITY_CHANGED,
                    "repository_id": rebound["repository_id"],
                    "location_id": rebound["location_id"],
                    "previous_repository_id": old_repository_id,
                }

            with immediate_tx(lambda: self.connect()) as write_conn:
                repository_id = find_or_create_repository(
                    write_conn,
                    backend=backend,
                    backend_repository_id=backend_repository_id,
                    now=now,
                )
                location_id = upsert_location(
                    write_conn,
                    repository_id=repository_id,
                    repository=display_repository,
                    repository_location_key=repository_location_key,
                    now=now,
                    successful=True,
                )
            return {
                "status": "new_location",
                "repository_id": repository_id,
                "location_id": location_id,
            }

    def connect(self) -> sqlite3.Connection:
        return self._connect()

    def find_or_create_repository(
        self,
        backend: str,
        backend_repository_id: str,
        first_seen_at: str | None = None,
    ) -> str:
        """Find or create a repository using its backend natural key."""
        with immediate_tx(lambda: self.connect()) as conn:
            return find_or_create_repository(
                conn,
                backend=backend,
                backend_repository_id=backend_repository_id,
                now=first_seen_at,
            )

    def upsert_location(
        self,
        repository_id: str,
        repository_location_key: str,
        display_repository: str,
        seen_at: str | None = None,
        *,
        successful: bool = True,
    ) -> str:
        """Upsert a location through the public store primitive."""
        with immediate_tx(lambda: self.connect()) as conn:
            return upsert_location(
                conn,
                repository_id=repository_id,
                repository=display_repository,
                repository_location_key=repository_location_key,
                now=seen_at,
                successful=successful,
            )

    def insert_artifact(
        self,
        repository_id: str,
        artifact: dict[str, object],
        first_seen_at: str | None = None,
    ) -> str:
        with immediate_tx(lambda: self.connect()) as conn:
            return self._insert_artifact_payload(
                conn,
                repository_id=repository_id,
                artifact=artifact,
                first_seen_at=first_seen_at,
            )

    def upsert_location_observation(
        self,
        artifact_id: str,
        location_id: str,
        seen_at: str | None = None,
        *,
        present: bool = True,
    ) -> None:
        """Upsert an artifact-location observation."""
        with immediate_tx(lambda: self.connect()) as conn:
            if present:
                upsert_location_observation(conn, artifact_id=artifact_id, location_id=location_id)
            else:
                observed_at = _canonical_time_text(seen_at) or _now_text()
                conn.execute(
                    """
                    INSERT INTO artifact_locations (
                        artifact_location_id, artifact_id, location_id, present,
                        removed_at, removed_by_run_step_id
                    )
                    VALUES (?, ?, ?, 0, ?, NULL)
                    ON CONFLICT(artifact_id, location_id) DO UPDATE SET
                        present = 0,
                        removed_at = excluded.removed_at,
                        removed_by_run_step_id = NULL
                    """,
                    (
                        str(uuid4()),
                        artifact_id,
                        location_id,
                        observed_at,
                    ),
                )

    def insert_stats_point(
        self,
        conn: sqlite3.Connection,
        *,
        repository_id: str,
        location_id: str | None,
        run_step_id: str | None,
        trigger_kind: str,
        collected_at: str | None = None,
        stats: dict[str, object] | None = None,
    ) -> str:
        return insert_stats_point(
            conn,
            repository_id=repository_id,
            location_id=location_id,
            run_step_id=run_step_id,
            trigger_kind=trigger_kind,
            collected_at=collected_at,
            stats=stats,
        )

    def resolve_repository(
        self,
        job: str,
        backup: str,
        *,
        backend: str = "restic",
        backend_repository_id: str | None = None,
    ) -> dict[str, object] | None:
        """Resolve ``job.backup`` to its active repository context.

        ``backend_repository_id`` is an explicit fallback for callers that have
        already contacted the backend but have no stored context yet.
        """
        _validate_name(job, "job")
        _validate_name(backup, "backup")
        with closing(self._connect()) as conn:
            if backend_repository_id is None:
                return None
            row = conn.execute(
                """
                SELECT repository_id, backend, backend_repository_id
                FROM repositories
                WHERE backend = ? AND backend_repository_id = ?
                """,
                (backend, backend_repository_id),
            ).fetchone()
            return _row_dict(row) if row is not None else None

    def resolve_location(self, repository_location_key: str) -> dict[str, object] | None:
        with closing(self._connect()) as conn:
            row = _location_by_key(conn, repository_location_key)
            return _repository_resolution(row) if row is not None else None

    def find_location(self, location_id: str) -> dict[str, object] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT locations.location_id, locations.repository_id,
                       repositories.backend, repositories.backend_repository_id,
                       locations.repository_location_key, locations.display_repository
                FROM repository_locations AS locations
                JOIN repositories ON repositories.repository_id = locations.repository_id
                WHERE locations.location_id = ?
                """,
                (location_id,),
            ).fetchone()
            return _row_dict(row) if row is not None else None

    def list_present_artifacts(
        self,
        repository_id: str,
        *,
        location_id: str | None = None,
        at: str | None = None,
    ) -> list[dict[str, object]]:
        """List artifacts present at active locations for a repository."""
        params: list[object] = [
            repository_id,
        ]
        location_filter = ""
        if location_id is not None:
            location_filter = "AND locations.location_id = ?"
            params.append(location_id)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT DISTINCT a.*, locations.location_id, locations.display_repository
                FROM artifacts AS a
                JOIN artifact_locations AS artifact_locations
                  ON artifact_locations.artifact_id = a.artifact_id
                JOIN repository_locations AS locations
                  ON locations.location_id = artifact_locations.location_id
                WHERE a.repository_id = ?
                  AND artifact_locations.present = 1
                  {location_filter}
                ORDER BY a.created_at DESC, a.backend_artifact_id DESC
                """,
                tuple(params),
            ).fetchall()
            return [self._artifact_detail_from_row(row) for row in rows]

    def growth_points(
        self, repository_id: str, *, location_id: str | None = None
    ) -> list[dict[str, object]]:
        params: list[object] = [repository_id]
        location_filter = ""
        if location_id is not None:
            location_filter = "AND location_id = ?"
            params.append(location_id)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM repository_stats_points
                WHERE repository_id = ?
                  {location_filter}
                ORDER BY collected_at ASC, stats_point_id ASC
                """,
                tuple(params),
            ).fetchall()
            return [_row_dict(row) for row in rows]

    def list_locations(self, repository_id: str) -> list[dict[str, object]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM repository_locations
                WHERE repository_id = ?
                ORDER BY display_repository ASC, location_id ASC
                """,
                (repository_id,),
            ).fetchall()
            return [_row_dict(row) for row in rows]

    def list_repositories(self) -> list[dict[str, object]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT repository_id, backend, backend_repository_id FROM repositories"
            ).fetchall()
            repositories = [_row_dict(row) for row in rows]
        for repository in repositories:
            repository["locations"] = self.list_locations(str(repository["repository_id"]))
        return repositories

    def delete_location(self, repository_id: str, location_id: str) -> bool:
        """Hard-delete one repository location and orphaned inactive artifacts."""
        with immediate_tx(lambda: self.connect()) as conn:
            cursor = conn.execute(
                """
                DELETE FROM repository_locations
                WHERE repository_id = ? AND location_id = ?
                """,
                (repository_id, location_id),
            )
            if cursor.rowcount > 0:
                _delete_orphaned_artifacts(conn, repository_id)
                _delete_empty_repository(conn, repository_id)
            return cursor.rowcount > 0

    def merge_location(
        self, repository_id: str, source_location_id: str, target_location_id: str
    ) -> bool:
        """Merge one location's observations and growth points into another location."""
        try:
            with immediate_tx(lambda: self.connect()) as conn:
                source = _location_by_id(conn, repository_id, source_location_id)
                target = _location_by_id(conn, repository_id, target_location_id)
                if source is None or target is None or source_location_id == target_location_id:
                    raise _MergeLocationNoOpError()
                _merge_artifact_locations(
                    conn,
                    source_location_id=source_location_id,
                    target_location_id=target_location_id,
                )
                conn.execute(
                    """
                    UPDATE repository_stats_points
                    SET location_id = ?
                    WHERE repository_id = ? AND location_id = ?
                    """,
                    (target_location_id, repository_id, source_location_id),
                )
                conn.execute(
                    """
                    DELETE FROM repository_locations
                    WHERE repository_id = ? AND location_id = ?
                    """,
                    (repository_id, source_location_id),
                )
                _delete_orphaned_artifacts(conn, repository_id)
                _delete_empty_repository(conn, repository_id)
                return True
        except _MergeLocationNoOpError:
            return False

    def read_backup_view(self, job: str, backup: str) -> dict[str, object] | None:
        resolution = self.resolve_repository(job, backup)
        if resolution is None:
            with closing(self._connect()) as conn:
                rows = conn.execute("""
                    SELECT locations.location_id, locations.repository_id,
                           repositories.backend, repositories.backend_repository_id,
                           locations.repository_location_key, locations.display_repository
                    FROM repository_locations AS locations
                    JOIN repositories
                      ON repositories.repository_id = locations.repository_id
                    ORDER BY locations.display_repository ASC, locations.location_id ASC
                    LIMIT 2
                    """).fetchall()
            if len(rows) != 1:
                return None
            resolution = _repository_resolution(rows[0])
        return self._read_backup_view_from_resolution(job, backup, resolution)

    def read_backup_view_for_location(
        self, job: str, backup: str, repository_location_key: str
    ) -> dict[str, object] | None:
        resolution = self.resolve_location(repository_location_key)
        if resolution is None:
            return None
        return self._read_backup_view_from_resolution(job, backup, resolution)

    def _read_backup_view_from_resolution(
        self, job: str, backup: str, resolution: dict[str, object]
    ) -> dict[str, object]:
        repository_id = str(resolution["repository_id"])
        location_id = str(resolution["location_id"])
        artifacts = self.list_present_artifacts(repository_id, location_id=location_id)
        points = self.growth_points(repository_id, location_id=location_id)
        return _backup_view_from_artifacts(
            job=job,
            backup=backup,
            repository=str(resolution.get("display_repository") or ""),
            artifacts=artifacts,
            points=points,
        )

    def prune_locations_by_count(
        self,
        repository_id: str,
        *,
        limit: int = DEFAULT_LOCATION_LIMIT,
        protected_location_keys: frozenset[str] = frozenset(),
    ) -> None:
        """Keep only the newest ``limit`` repository locations, oldest deleted first (FIFO).

        Locations referenced by the active config (``protected_location_keys``)
        are never deleted and never count against ``limit``.
        """
        with immediate_tx(lambda: self.connect()) as conn:
            _prune_locations_by_count(
                conn,
                repository_id,
                limit=limit,
                protected_location_keys=protected_location_keys,
            )

    def prune_artifacts(
        self,
        *,
        retention_days: int | None = None,
        retention_count: int | None = None,
        at: str | None = None,
        now: str | None = None,
    ) -> None:
        """Delete retained artifacts that are no longer present at active locations."""
        base_time_text = at or now
        observed_at = _canonical_time_text(base_time_text) or _now_text()
        with immediate_tx(lambda: self.connect()) as conn:
            _prune_artifacts(
                conn,
                retention_days=(
                    retention_days if retention_days is not None else self._retention_days
                ),
                retention_count=(
                    retention_count if retention_count is not None else self._retention_count
                ),
                at=observed_at,
            )

    def is_retention_due(self, repository_id: str, *, at: str | None = None) -> bool:
        observed_at = _canonical_time_text(at) or _now_text()
        retention_date = _retention_date(observed_at)
        with closing(self._connect()) as conn:
            return _last_retention_date(conn, repository_id) != retention_date

    def _connect(self) -> sqlite3.Connection:
        return connect_appdata_db(self.db_path)

    def _persist_observation(
        self,
        conn: sqlite3.Connection,
        *,
        job: str,
        backup: str,
        display_repository: str,
        repository_location_key: str,
        backend: str,
        backend_repository_id: str,
        artifacts: list[dict[str, object]],
        stats: dict[str, object] | None,
        trigger_kind: str,
        run_step_id: str | None,
        backup_summary: dict[str, object] | None,
        observed_at: str,
        full_reconcile: bool,
    ) -> dict[str, object]:
        repository_id = find_or_create_repository(
            conn, backend=backend, backend_repository_id=backend_repository_id, now=observed_at
        )
        location_id = upsert_location(
            conn,
            repository_id=repository_id,
            repository=display_repository,
            repository_location_key=repository_location_key,
            now=observed_at,
            successful=True,
        )
        newest_backend_artifact_id: str | None = None
        if trigger_kind == "backup" and artifacts:
            newest_snapshot = max(artifacts, key=_artifact_time)
            newest_backend_artifact_id = _backend_artifact_id(newest_snapshot)

        seen_artifact_ids: set[str] = set()
        created_artifact_id: str | None = None
        for snapshot in artifacts:
            backend_artifact_id = _backend_artifact_id(snapshot)
            if backend_artifact_id is None:
                continue
            artifact_id = self._insert_artifact_payload(
                conn,
                repository_id=repository_id,
                artifact=snapshot,
                first_seen_at=observed_at,
                include_summary=True,
            )
            upsert_location_observation(
                conn,
                artifact_id=artifact_id,
                location_id=location_id,
            )
            seen_artifact_ids.add(artifact_id)
            if (
                trigger_kind == "backup"
                and created_artifact_id is None
                and backend_artifact_id == newest_backend_artifact_id
            ):
                created_artifact_id = artifact_id
                if backup_summary is not None:
                    update_backup_summary_diagnostics(
                        conn, artifact_id=artifact_id, summary=backup_summary
                    )
        linked_run_step_id = _existing_run_step_id(conn, run_step_id)
        if full_reconcile:
            _mark_missing_artifacts_removed(
                conn,
                location_id=location_id,
                present_artifact_ids=seen_artifact_ids,
                removed_at=observed_at,
                removed_by_run_step_id=linked_run_step_id,
            )
        if created_artifact_id is not None and linked_run_step_id is not None:
            conn.execute(
                """
                UPDATE artifacts
                SET created_run_step_id = COALESCE(created_run_step_id, ?)
                WHERE artifact_id = ?
                """,
                (linked_run_step_id, created_artifact_id),
            )
        stats_point_id: str | None = None
        if trigger_kind != "retention" and stats is not None:
            stats_point_id = self.insert_stats_point(
                conn,
                repository_id=repository_id,
                location_id=location_id,
                run_step_id=linked_run_step_id,
                trigger_kind=trigger_kind,
                collected_at=observed_at,
                stats=stats,
            )
        return {
            "repository_id": repository_id,
            "location_id": location_id,
            "artifact_id": created_artifact_id,
            "stats_point_id": stats_point_id,
        }

    def _apply_retention_if_due(
        self,
        conn: sqlite3.Connection,
        *,
        repository_id: str,
        at: str,
        protected_location_keys: frozenset[str] | None = frozenset(),
    ) -> None:
        if protected_location_keys is None:
            return
        if self._retention_days is None and self._retention_count is None:
            return
        retention_date = _retention_date(at)
        if _last_retention_date(conn, repository_id) == retention_date:
            return
        _prune_locations_by_count(
            conn,
            repository_id,
            limit=DEFAULT_LOCATION_LIMIT,
            protected_location_keys=protected_location_keys,
        )
        _prune_artifacts(
            conn,
            retention_days=self._retention_days,
            retention_count=self._retention_count,
            at=at,
        )
        _mark_retention_date(conn, repository_id, retention_date)

    def _insert_artifact_payload(
        self,
        conn: sqlite3.Connection,
        *,
        repository_id: str,
        artifact: dict[str, object],
        first_seen_at: str | None,
        include_summary: bool = True,
    ) -> str:
        backend_artifact_id = _backend_artifact_id(artifact)
        if backend_artifact_id is None:
            raise ServiceError("invalid_parameter", "Artifact lacks backend ID", 400)
        artifact_id = insert_artifact(
            conn,
            repository_id=repository_id,
            backend_artifact_id=backend_artifact_id,
            backend_artifact_short_id=_optional_text(artifact.get("backend_artifact_short_id"))
            or _optional_text(artifact.get("short_id"))
            or backend_artifact_id[:8],
            created_at=_optional_text(artifact.get("created_at"))
            or _optional_text(artifact.get("time")),
            first_seen_at=first_seen_at,
        )
        metadata = artifact.get("metadata")
        summary = artifact.get("summary")
        insert_paths(conn, artifact_id=artifact_id, values=artifact.get("paths"))
        insert_tags(conn, artifact_id=artifact_id, values=artifact.get("tags"))
        update_artifact_details(
            conn,
            artifact_id=artifact_id,
            metadata=metadata if isinstance(metadata, dict) else artifact,
            summary=summary if include_summary and isinstance(summary, dict) else None,
            raw_snapshot=artifact,
        )
        return artifact_id

    def _artifact_detail_from_row(self, row: sqlite3.Row) -> dict[str, object]:
        detail = _row_dict(row)
        detail["metadata"] = _metadata_from_artifact_row(row)
        detail["summary"] = _summary_from_artifact_row(row)
        detail["paths"] = _json_value(detail.get("paths_json")) or []
        detail["tags"] = _json_value(detail.get("tags_json")) or []
        return {key: value for key, value in detail.items() if value is not None}


def repository_location_key(repository: str) -> str:
    return canonical_resource_id(repository, allow_direct_rclone=True)


def repository_location_key_for_display(repository: str) -> str:
    return repository_location_key(repository)


def find_or_create_repository(
    conn: sqlite3.Connection, *, backend: str, backend_repository_id: str, now: str | None = None
) -> str:
    """Find or create a repository and resolve the canonical UUID by natural key."""
    conn.execute(
        """
        INSERT INTO repositories (repository_id, backend, backend_repository_id)
        VALUES (?, ?, ?)
        ON CONFLICT(backend, backend_repository_id) DO NOTHING
        """,
        (str(uuid4()), backend, backend_repository_id),
    )
    row = conn.execute(
        """
        SELECT repository_id
        FROM repositories
        WHERE backend = ? AND backend_repository_id = ?
        """,
        (backend, backend_repository_id),
    ).fetchone()
    if row is None:
        raise RuntimeError("Repository find-or-create failed")
    return str(row["repository_id"])


def upsert_location(
    conn: sqlite3.Connection,
    *,
    repository_id: str,
    repository: str,
    repository_location_key: str,
    now: str | None = None,
    successful: bool = True,
) -> str:
    """Upsert a repository location and return its canonical ID.

    ``location_id`` is intentionally not a plain random UUID: its lexicographic
    order matches creation order, which ``_prune_locations_by_count`` relies on
    for FIFO retention without a separate timestamp column.
    """
    conn.execute(
        """
        INSERT INTO repository_locations (
            location_id, repository_id, repository_location_key, display_repository
        )
        VALUES (?, ?, ?, ?)
        ON CONFLICT(repository_location_key) DO UPDATE SET
            repository_id = excluded.repository_id,
            display_repository = excluded.display_repository
        """,
        (
            _new_sortable_id(),
            repository_id,
            repository_location_key,
            repository,
        ),
    )
    row = conn.execute(
        """
        SELECT location_id
        FROM repository_locations
        WHERE repository_id = ? AND repository_location_key = ?
        """,
        (repository_id, repository_location_key),
    ).fetchone()
    if row is None:
        raise RuntimeError("Location upsert failed")
    return str(row["location_id"])


def insert_artifact(
    conn: sqlite3.Connection,
    *,
    repository_id: str,
    backend_artifact_id: str,
    backend_artifact_short_id: str | None,
    created_at: str | None,
    first_seen_at: str | None = None,
) -> str:
    created_text = _canonical_time_text(created_at or first_seen_at) or _now_text()
    conn.execute(
        """
        INSERT INTO artifacts (
            artifact_id, repository_id, backend, backend_artifact_id,
            backend_artifact_short_id, created_at, paths_json, tags_json,
            raw_snapshot_json
        )
        VALUES (?, ?, 'restic', ?, ?, ?, '[]', '[]', '{}')
        ON CONFLICT(repository_id, backend_artifact_id) DO NOTHING
        """,
        (
            str(uuid4()),
            repository_id,
            backend_artifact_id,
            backend_artifact_short_id,
            created_text,
        ),
    )
    row = conn.execute(
        """
        SELECT artifact_id
        FROM artifacts
        WHERE repository_id = ? AND backend_artifact_id = ?
        """,
        (repository_id, backend_artifact_id),
    ).fetchone()
    if row is None:
        raise RuntimeError("Artifact insert failed")
    return str(row["artifact_id"])


def update_backup_summary_diagnostics(
    conn: sqlite3.Connection, *, artifact_id: str, summary: dict[str, object]
) -> None:
    """Attach the small restic backup summary as raw diagnostics."""
    conn.execute(
        """
        UPDATE artifacts
        SET raw_backup_output_json = COALESCE(raw_backup_output_json, ?)
        WHERE artifact_id = ?
        """,
        (
            _json_text(summary),
            artifact_id,
        ),
    )


def _summary_update_values(summary: dict[str, object]) -> tuple[object, ...]:
    duration = compute_duration(summary)
    return (
        _optional_text(summary.get("backup_start")),
        _optional_text(summary.get("backup_end")),
        _optional_float(duration),
        _optional_int(summary.get("files_new")),
        _optional_int(summary.get("files_changed")),
        _optional_int(summary.get("files_unmodified")),
        _optional_int(summary.get("dirs_new")),
        _optional_int(summary.get("dirs_changed")),
        _optional_int(summary.get("dirs_unmodified")),
        _optional_int(summary.get("data_added")),
        _optional_int(summary.get("data_added_packed")),
        _optional_int(summary.get("total_files_processed")),
        _optional_int(summary.get("total_bytes_processed")),
    )


def insert_paths(conn: sqlite3.Connection, *, artifact_id: str, values: object) -> None:
    if isinstance(values, list):
        conn.execute(
            "UPDATE artifacts SET paths_json = ? WHERE artifact_id = ? AND paths_json = '[]'",
            (
                _json_text([value for value in values if isinstance(value, str)]),
                artifact_id,
            ),
        )


def insert_tags(conn: sqlite3.Connection, *, artifact_id: str, values: object) -> None:
    if isinstance(values, list):
        conn.execute(
            "UPDATE artifacts SET tags_json = ? WHERE artifact_id = ? AND tags_json = '[]'",
            (
                _json_text([value for value in values if isinstance(value, str)]),
                artifact_id,
            ),
        )


def update_artifact_details(
    conn: sqlite3.Connection,
    *,
    artifact_id: str,
    metadata: dict[str, object],
    summary: dict[str, object] | None,
    raw_snapshot: dict[str, object],
    raw_backup_output: dict[str, object] | None = None,
) -> None:
    """Fill canonical artifact columns from restic snapshot/summary payloads."""
    summary_values = _summary_update_values(summary) if summary is not None else (None,) * 13
    conn.execute(
        """
        UPDATE artifacts
        SET hostname = COALESCE(hostname, ?),
            backup_start = COALESCE(backup_start, ?),
            backup_end = COALESCE(backup_end, ?),
            duration_seconds = COALESCE(duration_seconds, ?),
            files_new = COALESCE(files_new, ?),
            files_changed = COALESCE(files_changed, ?),
            files_unmodified = COALESCE(files_unmodified, ?),
            dirs_new = COALESCE(dirs_new, ?),
            dirs_changed = COALESCE(dirs_changed, ?),
            dirs_unmodified = COALESCE(dirs_unmodified, ?),
            data_added_bytes = COALESCE(data_added_bytes, ?),
            data_added_packed_bytes = COALESCE(data_added_packed_bytes, ?),
            total_files_processed = COALESCE(total_files_processed, ?),
            total_bytes_processed = COALESCE(total_bytes_processed, ?),
            raw_snapshot_json = CASE WHEN ? != '{}' THEN ? ELSE raw_snapshot_json END,
            raw_backup_output_json = COALESCE(raw_backup_output_json, ?)
        WHERE artifact_id = ?
        """,
        (
            _optional_text(metadata.get("hostname")),
            *summary_values,
            _json_text(raw_snapshot),
            _json_text(raw_snapshot),
            _json_text(raw_backup_output) if raw_backup_output is not None else None,
            artifact_id,
        ),
    )


def upsert_location_observation(
    conn: sqlite3.Connection, *, artifact_id: str, location_id: str
) -> None:
    """Mark an artifact as present at a location."""
    conn.execute(
        """
        INSERT INTO artifact_locations (
            artifact_location_id, artifact_id, location_id, present,
            removed_at, removed_by_run_step_id
        )
        VALUES (?, ?, ?, 1, NULL, NULL)
        ON CONFLICT(artifact_id, location_id) DO UPDATE SET
            present = 1,
            removed_at = NULL,
            removed_by_run_step_id = NULL
        """,
        (str(uuid4()), artifact_id, location_id),
    )


def insert_stats_point(
    conn: sqlite3.Connection,
    *,
    repository_id: str,
    location_id: str | None,
    run_step_id: str | None,
    trigger_kind: str,
    collected_at: str | None = None,
    stats: dict[str, object] | None = None,
) -> str:
    stats_point_id = str(uuid4())
    raw_stats = stats or {}
    conn.execute(
        """
        INSERT INTO repository_stats_points (
            stats_point_id, repository_id, location_id, run_step_id, trigger,
            collected_at, total_size_bytes, artifacts_count, raw_stats_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(stats_point_id) DO NOTHING
        """,
        (
            stats_point_id,
            repository_id,
            location_id,
            run_step_id,
            trigger_kind,
            _canonical_time_text(collected_at) or _now_text(),
            _optional_int(_first_present(raw_stats, "total_size_bytes", "total_size")),
            _optional_int(_first_present(raw_stats, "artifacts_count", "snapshots_count")),
            _json_text(raw_stats),
        ),
    )
    return stats_point_id


def _mark_missing_artifacts_removed(
    conn: sqlite3.Connection,
    *,
    location_id: str,
    present_artifact_ids: set[str],
    removed_at: str,
    removed_by_run_step_id: str | None,
) -> None:
    rows = conn.execute(
        """
        SELECT artifact_id
        FROM artifact_locations
        WHERE location_id = ? AND present = 1
        """,
        (location_id,),
    ).fetchall()
    missing = [
        str(row["artifact_id"]) for row in rows if row["artifact_id"] not in present_artifact_ids
    ]
    for artifact_id in missing:
        conn.execute(
            """
            UPDATE artifact_locations
            SET present = 0, removed_at = ?, removed_by_run_step_id = ?
            WHERE artifact_id = ? AND location_id = ?
            """,
            (removed_at, removed_by_run_step_id, artifact_id, location_id),
        )


def _delete_locations(conn: sqlite3.Connection, location_ids: list[str]) -> None:
    if not location_ids:
        return
    conn.execute(
        "DELETE FROM repository_locations " f"WHERE location_id IN ({_placeholders(location_ids)})",
        tuple(location_ids),
    )


def _retention_meta_key(repository_id: str) -> str:
    return f"{ARTIFACT_RETENTION_META_PREFIX}{repository_id}"


def _retention_date(at: str) -> str:
    value = _datetime_from_text(at)
    if value is None:
        return at[:10]
    return value.date().isoformat()


def _last_retention_date(conn: sqlite3.Connection, repository_id: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM appdata_meta WHERE key = ?",
        (_retention_meta_key(repository_id),),
    ).fetchone()
    return str(row["value"]) if row is not None else None


def _mark_retention_date(conn: sqlite3.Connection, repository_id: str, retention_date: str) -> None:
    conn.execute(
        """
        INSERT INTO appdata_meta (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (_retention_meta_key(repository_id), retention_date),
    )


def _prune_locations_by_count(
    conn: sqlite3.Connection,
    repository_id: str,
    *,
    limit: int,
    protected_location_keys: frozenset[str] = frozenset(),
) -> None:
    """Delete the oldest locations beyond ``limit``, oldest first (FIFO).

    Locations whose ``repository_location_key`` is in ``protected_location_keys``
    (i.e. currently referenced by some job.backup in the active config) are
    never candidates for deletion and never count against ``limit`` — they are
    excluded from the ranking entirely, regardless of ``location_id`` age.
    """
    exclusion = ""
    params: list[object] = [repository_id]
    if protected_location_keys:
        exclusion = f"AND repository_location_key NOT IN ({_placeholders(protected_location_keys)})"
        params.extend(sorted(protected_location_keys))
    rows = conn.execute(
        f"""
        SELECT location_id
        FROM repository_locations
        WHERE repository_id = ?
        {exclusion}
        ORDER BY location_id DESC
        LIMIT -1 OFFSET ?
        """,
        (*params, limit),
    ).fetchall()
    _delete_locations(conn, [str(row["location_id"]) for row in rows])


def _delete_artifacts(conn: sqlite3.Connection, artifact_ids: set[str]) -> None:
    if not artifact_ids:
        return
    conn.execute(
        f"DELETE FROM artifacts WHERE artifact_id IN ({_placeholders(artifact_ids)})",
        tuple(sorted(artifact_ids)),
    )


def _prune_artifacts(
    conn: sqlite3.Connection,
    *,
    retention_days: int | None,
    retention_count: int | None,
    at: str,
) -> None:
    if retention_days is None and retention_count is None:
        return
    base_time = _datetime_from_text(at) or datetime.now(timezone.utc)
    removable = _removable_artifact_ids(conn)
    delete_ids: set[str] = set()
    if removable and retention_days is not None:
        cutoff = utc_rfc3339(base_time - timedelta(days=retention_days))
        old_rows = conn.execute(
            f"""
            SELECT artifact_id
            FROM artifacts
            WHERE artifact_id IN ({_placeholders(removable)})
              AND created_at < ?
            """,
            (*sorted(removable), cutoff),
        ).fetchall()
        delete_ids.update(str(row["artifact_id"]) for row in old_rows)
    if removable and retention_count is not None:
        rows = conn.execute(
            f"""
            SELECT artifact_id
            FROM artifacts
            WHERE artifact_id IN ({_placeholders(removable)})
            ORDER BY created_at DESC, artifact_id DESC
            LIMIT -1 OFFSET ?
            """,
            (*sorted(removable), retention_count),
        ).fetchall()
        delete_ids.update(str(row["artifact_id"]) for row in rows)
    _delete_artifacts(conn, delete_ids)


def _removable_artifact_ids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        """
        SELECT artifact_id
        FROM artifacts
        WHERE artifact_id NOT IN (
            SELECT artifact_locations.artifact_id
            FROM artifact_locations
            WHERE artifact_locations.present = 1
        )
        """,
    ).fetchall()
    return {str(row["artifact_id"]) for row in rows}


def _repository_resolution(row: sqlite3.Row) -> dict[str, object]:
    return {
        "repository_id": row["repository_id"],
        "location_id": row["location_id"],
        "backend": row["backend"],
        "backend_repository_id": row["backend_repository_id"],
        "repository_location_key": row["repository_location_key"],
        "display_repository": row["display_repository"],
    }


def _location_by_key(conn: sqlite3.Connection, repository_location_key: str) -> sqlite3.Row | None:
    row = conn.execute(
        """
        SELECT locations.location_id, locations.repository_id,
               repositories.backend, repositories.backend_repository_id,
               locations.repository_location_key, locations.display_repository
        FROM repository_locations AS locations
        JOIN repositories ON repositories.repository_id = locations.repository_id
        WHERE locations.repository_location_key = ?
        """,
        (repository_location_key,),
    ).fetchone()
    return row if isinstance(row, sqlite3.Row) else None


def _location_by_id(
    conn: sqlite3.Connection, repository_id: str, location_id: str
) -> sqlite3.Row | None:
    row = conn.execute(
        """
        SELECT locations.location_id, locations.repository_id,
               repositories.backend, repositories.backend_repository_id,
               locations.repository_location_key, locations.display_repository
        FROM repository_locations AS locations
        JOIN repositories ON repositories.repository_id = locations.repository_id
        WHERE locations.repository_id = ? AND locations.location_id = ?
        """,
        (repository_id, location_id),
    ).fetchone()
    return row if isinstance(row, sqlite3.Row) else None


def _merge_artifact_locations(
    conn: sqlite3.Connection, *, source_location_id: str, target_location_id: str
) -> None:
    rows = conn.execute(
        """
        SELECT *
        FROM artifact_locations
        WHERE location_id = ?
        """,
        (source_location_id,),
    ).fetchall()
    for source in rows:
        artifact_id = str(source["artifact_id"])
        target = conn.execute(
            """
            SELECT *
            FROM artifact_locations
            WHERE artifact_id = ? AND location_id = ?
            """,
            (artifact_id, target_location_id),
        ).fetchone()
        if target is None:
            conn.execute(
                """
                UPDATE artifact_locations
                SET location_id = ?
                WHERE artifact_location_id = ?
                """,
                (target_location_id, source["artifact_location_id"]),
            )
            continue

        present = 1 if int(source["present"]) == 1 or int(target["present"]) == 1 else 0
        removed_at = None
        removed_by_run_step_id = None
        if present == 0:
            removed_values = [
                str(value)
                for value in (source["removed_at"], target["removed_at"])
                if value is not None
            ]
            removed_at = max(removed_values) if removed_values else None
            removed_by_run_step_id = (
                target["removed_by_run_step_id"] or source["removed_by_run_step_id"]
            )
        conn.execute(
            """
            UPDATE artifact_locations
            SET present = ?,
                removed_at = ?,
                removed_by_run_step_id = ?
            WHERE artifact_location_id = ?
            """,
            (
                present,
                removed_at,
                removed_by_run_step_id,
                target["artifact_location_id"],
            ),
        )
        conn.execute(
            "DELETE FROM artifact_locations WHERE artifact_location_id = ?",
            (source["artifact_location_id"],),
        )


def _rebind_location_identity(
    conn: sqlite3.Connection,
    *,
    backend: str,
    backend_repository_id: str,
    repository_location_key: str,
    display_repository: str,
    old_repository_id: str,
    now: str,
) -> None:
    old_location = _location_by_key(conn, repository_location_key)
    if old_location is not None:
        conn.execute(
            "DELETE FROM repository_locations WHERE location_id = ?",
            (old_location["location_id"],),
        )
    _delete_orphaned_artifacts(conn, old_repository_id)
    _delete_empty_repository(conn, old_repository_id)
    repository_id = find_or_create_repository(
        conn, backend=backend, backend_repository_id=backend_repository_id, now=now
    )
    upsert_location(
        conn,
        repository_id=repository_id,
        repository=display_repository,
        repository_location_key=repository_location_key,
        now=now,
        successful=True,
    )


def _delete_orphaned_artifacts(conn: sqlite3.Connection, repository_id: str) -> None:
    rows = conn.execute(
        """
        SELECT artifact_id
        FROM artifacts
        WHERE repository_id = ?
          AND artifact_id NOT IN (
              SELECT artifact_id
              FROM artifact_locations
              WHERE present = 1
          )
        """,
        (repository_id,),
    ).fetchall()
    _delete_artifacts(conn, {str(row["artifact_id"]) for row in rows})


def _delete_empty_repository(conn: sqlite3.Connection, repository_id: str) -> None:
    row = conn.execute(
        """
        SELECT 1
        WHERE NOT EXISTS (
            SELECT 1 FROM repository_locations WHERE repository_id = ?
        )
          AND NOT EXISTS (
            SELECT 1 FROM artifacts WHERE repository_id = ?
        )
          AND NOT EXISTS (
            SELECT 1 FROM repository_stats_points WHERE repository_id = ?
        )
        """,
        (repository_id, repository_id, repository_id),
    ).fetchone()
    if row is not None:
        conn.execute("DELETE FROM repositories WHERE repository_id = ?", (repository_id,))


def _existing_run_step_id(conn: sqlite3.Connection, run_step_id: str | None) -> str | None:
    if run_step_id is None:
        return None
    row = conn.execute(
        "SELECT run_step_id FROM run_steps WHERE run_step_id = ?",
        (run_step_id,),
    ).fetchone()
    return str(row["run_step_id"]) if row is not None else None


def _metadata_from_artifact_row(row: sqlite3.Row) -> dict[str, object]:
    raw = _json_value(row["raw_snapshot_json"])
    metadata = raw.get("metadata", raw) if isinstance(raw, dict) else {}
    result = dict(metadata) if isinstance(metadata, dict) else {}
    if row["hostname"] is not None:
        result.setdefault("hostname", row["hostname"])
    return result


def _summary_from_artifact_row(row: sqlite3.Row) -> dict[str, object] | None:
    raw = _json_value(row["raw_backup_output_json"])
    result = dict(raw) if isinstance(raw, dict) else {}
    for key, column in {
        "backup_start": "backup_start",
        "backup_end": "backup_end",
        "total_duration": "duration_seconds",
        "files_new": "files_new",
        "files_changed": "files_changed",
        "files_unmodified": "files_unmodified",
        "dirs_new": "dirs_new",
        "dirs_changed": "dirs_changed",
        "dirs_unmodified": "dirs_unmodified",
        "data_added": "data_added_bytes",
        "data_added_packed": "data_added_packed_bytes",
        "total_files_processed": "total_files_processed",
        "total_bytes_processed": "total_bytes_processed",
    }.items():
        if row[column] is not None:
            result.setdefault(key, row[column])
    return result or None


def _backup_view_from_artifacts(
    *,
    job: str,
    backup: str,
    repository: str,
    artifacts: list[dict[str, object]],
    points: list[dict[str, object]],
) -> dict[str, object]:
    snapshots = [_snapshot_view_from_artifact(artifact) for artifact in artifacts]
    latest_point = _latest_stats_point(points)
    raw_stats = (
        _json_value(latest_point.get("raw_stats_json")) if latest_point is not None else None
    )
    raw_stats_dict = raw_stats if isinstance(raw_stats, dict) else {}
    total_size = _optional_int(latest_point.get("total_size_bytes")) if latest_point else None
    total_file_count = _optional_int(raw_stats_dict.get("total_file_count"))
    artifacts_count = _optional_int(latest_point.get("artifacts_count")) if latest_point else None
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": latest_point.get("collected_at") if latest_point is not None else None,
        "job": job,
        "backup": backup,
        "repository": repository,
        "total_size_bytes": total_size,
        "total_file_count": total_file_count,
        "snapshots_count": artifacts_count if artifacts_count is not None else len(snapshots),
        "last_backup": _latest_backup_summary(snapshots),
        "growth_history": [
            {
                "time": point.get("collected_at"),
                "total_size_bytes": point.get("total_size_bytes"),
                "total_file_count": point.get("total_file_count"),
                "snapshots_count": point.get("artifacts_count"),
            }
            for point in points
            if point.get("total_size_bytes") is not None
        ],
        "snapshots": snapshots,
    }


def _snapshot_view_from_artifact(artifact: dict[str, object]) -> dict[str, object]:
    metadata = artifact.get("metadata")
    summary = artifact.get("summary")
    raw_metadata = metadata if isinstance(metadata, dict) else {}
    raw_summary = summary if isinstance(summary, dict) else None
    snapshot: dict[str, object] = {
        "id": artifact.get("backend_artifact_id"),
        "short_id": artifact.get("backend_artifact_short_id"),
        "repository": artifact.get("display_repository"),
        "time": artifact.get("created_at"),
        "tree": raw_metadata.get("tree"),
        "hostname": raw_metadata.get("hostname"),
        "username": raw_metadata.get("username"),
        "uid": raw_metadata.get("uid"),
        "gid": raw_metadata.get("gid"),
        "program_version": raw_metadata.get("program_version"),
        "paths": artifact.get("paths") if isinstance(artifact.get("paths"), list) else [],
        "tags": artifact.get("tags") if isinstance(artifact.get("tags"), list) else [],
        "present": True,
    }
    if raw_summary is not None:
        converted_summary = dict(raw_summary)
        duration = converted_summary.pop("total_duration_seconds", None)
        if duration is not None:
            converted_summary["total_duration"] = duration
        snapshot["summary"] = converted_summary
    return {key: value for key, value in snapshot.items() if value is not None}


def _latest_stats_point(points: list[dict[str, object]]) -> dict[str, object] | None:
    if not points:
        return None
    return sorted(
        points,
        key=lambda point: (
            _datetime_from_text(point.get("collected_at"))
            or datetime.min.replace(tzinfo=timezone.utc),
            str(point.get("stats_point_id") or ""),
        ),
    )[-1]


def _latest_backup_summary(snapshots: list[dict[str, object]]) -> dict[str, object] | None:
    with_summary = [snapshot for snapshot in snapshots if isinstance(snapshot.get("summary"), dict)]
    if not with_summary:
        return None
    newest = sorted(with_summary, key=_artifact_time)[-1]
    summary = newest["summary"]
    if not isinstance(summary, dict):
        return None
    return {
        "time": newest.get("time"),
        "duration_seconds": summary.get("total_duration"),
        "data_added_bytes": summary.get("data_added"),
        "files_new": summary.get("files_new"),
        "files_changed": summary.get("files_changed"),
        "files_unmodified": summary.get("files_unmodified"),
    }


def _list_child(
    conn: sqlite3.Connection, table: str, artifact_id: str, value_column: str
) -> list[str]:
    rows = conn.execute(
        f"""
        SELECT {value_column}
        FROM {table}
        WHERE artifact_id = ?
        ORDER BY position ASC
        """,
        (artifact_id,),
    ).fetchall()
    return [str(row[value_column]) for row in rows]


def _row_dict(row: sqlite3.Row) -> dict[str, object]:
    return {key: row[key] for key in row.keys()}


def _json_text(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _json_value(value: object) -> object | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed: object = json.loads(value)
        return parsed
    except json.JSONDecodeError:
        return None


def _first_present(data: dict[str, object], *keys: str) -> object | None:
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return None


def _backend_artifact_id(artifact: dict[str, object]) -> str | None:
    return (
        _optional_text(artifact.get("backend_artifact_id"))
        or _optional_text(artifact.get("id"))
        or _optional_text(artifact.get("snapshot_id"))
    )


def _stats_collected_at(stats: dict[str, object] | None) -> str | None:
    if stats is None:
        return None
    return _canonical_time_text(_optional_text(stats.get("collected_at")))


def _now_text() -> str:
    return utc_rfc3339(datetime.now(timezone.utc))


def _datetime_from_text(value: object) -> datetime | None:
    try:
        return parse_appdata_datetime(value)
    except ValueError:
        return None


def _canonical_time_text(value: object) -> str | None:
    parsed = _datetime_from_text(value)
    return utc_rfc3339(parsed) if parsed is not None else None


def _artifact_time(artifact: dict[str, object]) -> datetime:
    return (
        _datetime_from_text(artifact.get("created_at"))
        or _datetime_from_text(artifact.get("time"))
        or datetime.min.replace(tzinfo=timezone.utc)
    )


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_int(value: object) -> int | None:
    if value is None or not isinstance(value, str | bytes | bytearray | int | float):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: object) -> float | None:
    if value is None or not isinstance(value, str | bytes | bytearray | int | float):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _placeholders(values: set[str] | frozenset[str] | list[str]) -> str:
    return ",".join("?" for _ in values)


def _validate_name(value: str, kind: str) -> str:
    if not _NAME_RE.fullmatch(value):
        raise ServiceError("invalid_parameter", f"Invalid {kind} name: {value!r}", 400)
    return value
