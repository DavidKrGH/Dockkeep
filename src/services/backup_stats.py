"""Aggregated backup statistics for the GUI charts."""

import json
import logging
from typing import Any

from .backup_stats_collector import BackupStatsCollector
from .config import ConfigService
from .errors import ConfigServiceError
from .repository_artifact_store import (
    RepositoryArtifactStore,
    repository_location_key_for_display,
)

logger = logging.getLogger(__name__)


class BackupStatsService:
    """Aggregate restic snapshot summary data into chart viewmodels."""

    def __init__(
        self,
        store: RepositoryArtifactStore | None = None,
        collector: BackupStatsCollector | None = None,
        config_service: ConfigService | None = None,
    ) -> None:
        self._store = store
        self._collector = collector
        self._config_service = config_service

    def get_backup_stats(self, job: str, backup: str) -> dict[str, object] | None:
        """Return backup stats from cache only.

        Returns:
            Stats dict or None if no cache entry exists.
        """
        if self._store is None:
            return None
        resolution = self._location_resolution(job, backup)
        if resolution is None:
            return None
        return self._backup_view_from_resolution(job, backup, resolution)

    def _location_key_for(self, job: str, backup: str) -> str | None:
        if self._config_service is None:
            return None
        try:
            config = self._config_service.load_active_config()
        except ConfigServiceError:
            return None
        job_config = config.jobs.get(job)
        if job_config is None:
            return None
        backup_config = job_config.backup.get(backup)
        if backup_config is None:
            return None
        return repository_location_key_for_display(backup_config.repository)

    def _location_resolution(self, job: str, backup: str) -> dict[str, object] | None:
        if self._store is None:
            return None
        location_key = self._location_key_for(job, backup)
        if location_key is None:
            return None
        return self._store.resolve_location(location_key)

    def _backup_view_from_resolution(
        self, job: str, backup: str, resolution: dict[str, object]
    ) -> dict[str, object]:
        if self._store is None:
            return {}
        repository_id = str(resolution["repository_id"])
        location_id = str(resolution["location_id"])
        artifacts = self._store.list_present_artifacts(repository_id, location_id=location_id)
        points = self._store.growth_points(repository_id, location_id=location_id)
        snapshots = [artifact_snapshot_view(artifact) for artifact in artifacts]
        latest_point = _latest_stats_point(points)
        raw_stats = _raw_stats(latest_point)
        total_size = _optional_int(latest_point.get("total_size_bytes")) if latest_point else None
        total_file_count = _optional_int(raw_stats.get("total_file_count"))
        artifacts_count = (
            _optional_int(latest_point.get("artifacts_count")) if latest_point else None
        )
        return {
            "updated_at": latest_point.get("collected_at") if latest_point is not None else None,
            "job": job,
            "backup": backup,
            "repository_id": repository_id,
            "location_id": location_id,
            "repository": str(resolution.get("display_repository") or ""),
            "total_size_bytes": total_size,
            "total_file_count": total_file_count,
            "snapshots_count": artifacts_count if artifacts_count is not None else len(snapshots),
            "last_backup": _latest_backup_summary(snapshots),
            "growth_history": _growth_history(points),
            "snapshots": snapshots,
        }

    async def refresh_backup_stats(self, job: str, backup: str) -> dict[str, object] | None:
        """Trigger an explicit live stats collection and store refresh.

        Returns:
            Collected stats dict, or None if no collector is configured.
        """
        if self._collector is None:
            return None
        return await self._collector.collect_and_store_async(job, backup)

    def get_all_backup_stats(self) -> list[dict[str, object]]:
        if self._store is None or self._config_service is None:
            return []
        results: list[dict[str, object]] = []
        try:
            config = self._config_service.load_active_config()
        except ConfigServiceError:
            return []
        for job_name, job in config.jobs.items():
            for backup_name, backup in job.backup.items():
                location_key = repository_location_key_for_display(backup.repository)
                resolution = self._store.resolve_location(location_key)
                if resolution is not None:
                    view = self._backup_view_from_resolution(job_name, backup_name, resolution)
                    results.append(view)
        return results

    async def refresh_all_backup_stats(self) -> None:
        """Refresh live stats for every configured backup.

        Errors for individual backups are logged and isolated so that one
        unreachable repository cannot abort the refresh of the others.
        """
        if self._config_service is None:
            return
        try:
            config = self._config_service.load_active_config()
        except ConfigServiceError:
            return
        for job_name, job in config.jobs.items():
            for backup_name in job.backup:
                try:
                    await self.refresh_backup_stats(job_name, backup_name)
                except Exception:
                    logger.exception(
                        "Failed to refresh backup stats for %s.%s", job_name, backup_name
                    )

    def get_backup_growth_data(self, job: str, backup: str) -> dict[str, object]:
        """Return growth chart data for a single backup.

        Returns:
            dict with keys:
              has_data (bool)
              labels (list[str]): timestamps "YYYY-MM-DD HH:MM"
              data (list[int]): raw-data repo size at each collection point
              formatted (list[str]): human-readable sizes
        """
        _empty: dict[str, object] = {"has_data": False, "labels": [], "data": [], "formatted": []}

        stats = self.get_backup_stats(job, backup)
        if stats is None:
            return _empty

        history = stats.get("growth_history")
        if not isinstance(history, list) or not history:
            return _empty

        entries: list[dict[str, object]] = []
        for entry in history:
            if not isinstance(entry, dict):
                continue
            time_raw = str(entry.get("time", ""))
            size = entry.get("total_size_bytes")
            if not time_raw or size is None:
                continue
            entries.append({"time": time_raw, "size": int(size)})

        if not entries:
            return _empty

        entries.sort(key=lambda e: str(e.get("time", "")))

        labels: list[str] = []
        data: list[int] = []
        formatted: list[str] = []
        for e in entries:
            time_raw = str(e.get("time", ""))
            labels.append(time_raw[:16].replace("T", " "))
            size_value = e.get("size", 0)
            sz = int(size_value) if isinstance(size_value, (int, float)) else 0
            data.append(sz)
            formatted.append(_format_chart_bytes(sz))

        return {"has_data": True, "labels": labels, "data": data, "formatted": formatted}

    def get_dashboard_growth_data(self) -> dict[str, object]:
        """Return pre-processed growth chart data for the dashboard.

        Returns:
            dict with keys:
              all_labels (list[str]): Sorted unique timestamps ("YYYY-MM-DD HH:MM").
              datasets (list[dict]): Each dict has "label" (str) and "data" (list[int|None]).
        """
        if self._store is None:
            return {"all_labels": [], "datasets": []}

        all_stats = self.get_all_backup_stats()
        all_labels_set: set[str] = set()
        backup_histories: list[dict[str, object]] = []

        for stats in all_stats:
            history = stats.get("growth_history")
            if not isinstance(history, list) or not history:
                continue
            job = str(stats.get("job", ""))
            backup = str(stats.get("backup", ""))
            entries: list[dict[str, object]] = []
            for entry in history:
                if not isinstance(entry, dict):
                    continue
                time_raw = str(entry.get("time", ""))
                size = entry.get("total_size_bytes")
                if not time_raw or size is None:
                    continue
                label = time_raw[:16].replace("T", " ")
                all_labels_set.add(label)
                entries.append({"label": label, "size": int(size)})
            if entries:
                backup_histories.append({"label": f"{job}.{backup}", "entries": entries})

        sorted_labels = sorted(all_labels_set)
        datasets: list[dict[str, object]] = []
        for ph in backup_histories:
            raw_entries = ph.get("entries")
            if not isinstance(raw_entries, list):
                continue
            entry_map: dict[str, int] = {}
            for e in raw_entries:
                if isinstance(e, dict):
                    lbl = e.get("label")
                    sz = e.get("size")
                    if isinstance(lbl, str) and isinstance(sz, int):
                        entry_map[lbl] = sz
            data_points: list[int | None] = [entry_map.get(lbl) for lbl in sorted_labels]
            datasets.append({"label": str(ph.get("label", "")), "data": data_points})

        return {"all_labels": sorted_labels, "datasets": datasets}


def _format_chart_bytes(value: int) -> str:
    size = float(value)
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if abs(size) < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def artifact_snapshot_view(artifact: dict[str, object]) -> dict[str, object]:
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
            str(point.get("collected_at") or ""),
            str(point.get("stats_point_id") or ""),
        ),
    )[-1]


def _raw_stats(point: dict[str, object] | None) -> dict[str, Any]:
    if point is None:
        return {}
    raw = point.get("raw_stats_json")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _growth_history(points: list[dict[str, object]]) -> list[dict[str, object]]:
    history: list[dict[str, object]] = []
    for point in points:
        if point.get("total_size_bytes") is None:
            continue
        raw_stats = _raw_stats(point)
        history.append(
            {
                "time": point.get("collected_at"),
                "total_size_bytes": point.get("total_size_bytes"),
                "total_file_count": raw_stats.get("total_file_count"),
                "snapshots_count": point.get("artifacts_count"),
            }
        )
    return history


def _latest_backup_summary(snapshots: list[dict[str, object]]) -> dict[str, object] | None:
    with_summary = [snapshot for snapshot in snapshots if isinstance(snapshot.get("summary"), dict)]
    if not with_summary:
        return None
    newest = sorted(with_summary, key=lambda snapshot: str(snapshot.get("time") or ""))[-1]
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


def _optional_int(value: object) -> int | None:
    if value is None or value is True or value is False:
        return None
    if not isinstance(value, (int, float, str, bytes, bytearray)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
