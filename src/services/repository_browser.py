"""Repository browser view models backed by ``restic ls --json`` only."""

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict, cast
from urllib.parse import urlencode

from ..core.subprocesses import stream_command
from ..models.resolved_config import ResolvedAppConfig, ResolvedBackupConfig, ResolvedCredentials
from ..utils.restic import resolve_env
from ..utils.timeouts import env_timeout
from .backup_stats import BackupStatsService, artifact_snapshot_view
from .backup_stats_helpers import compute_duration
from .config import ConfigService, get_job_or_raise
from .errors import ConfigServiceError, NotFoundServiceError, ServiceError
from .output import limit_output
from .repository_artifact_store import (
    RepositoryArtifactStore,
    repository_location_key_for_display,
)

logger = logging.getLogger(__name__)

BROWSE_TIMEOUT_ENV = "DK_BROWSE_TIMEOUT"
DEFAULT_BROWSE_TIMEOUT = 30
BROWSE_PAGE_SIZE = 100
BROWSE_CACHE_TTL_SECONDS = 900
BROWSE_INDEX_CACHE_MAX_BYTES_TOTAL = 200 * 1024 * 1024
_BROWSE_CACHE_ENTRY_OVERHEAD_BYTES = 512
_STREAM_READ_SIZE = 65536
_STREAM_SAMPLE_LIMIT = 65536

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_SNAPSHOT_ID_RE = re.compile(r"^[A-Fa-f0-9]{8,64}$")
_BrowseCacheKey = tuple[str, str, str]


@dataclass(frozen=True)
class BrowseIndexGroup:
    """One complete user-triggered recursive browse index."""

    group_id: str
    repository: str
    snapshot_id: str
    root_path: str
    created_at: float
    estimated_bytes: int
    cache_keys: set[_BrowseCacheKey]


class ServiceErrorView(TypedDict):
    code: str
    message: str
    output_truncated: bool


@dataclass(frozen=True)
class ResticLsResult:
    """Internal result for ``restic ls --json`` commands."""

    ok: bool
    entries: list["BrowserEntryView"]
    error: ServiceErrorView | None = None


@dataclass(frozen=True)
class ResticRecursiveLsResult:
    """Internal result for recursive ``restic ls --json`` commands."""

    ok: bool
    directories: dict[str, list["BrowserEntryView"]]
    error: ServiceErrorView | None = None


class BreadcrumbView(TypedDict):
    name: str
    path: str
    browse_url: str


class BrowserEntryView(TypedDict, total=False):
    name: str
    path: str
    type: str
    size: object
    mode: object
    mtime: object
    browse: dict[str, str] | None
    browse_url: str | None


class LocationBrowseView(TypedDict):
    location_id: str
    snapshot_id: str
    path: str
    page: int
    page_size: int
    parent_path: str | None
    breadcrumbs: list[BreadcrumbView]
    prefetch_url: str
    indexed_paths: int
    indexed_bytes: int
    indexed_bytes_limit: int
    ok: bool
    entries: list[BrowserEntryView]
    directories: list[BrowserEntryView]
    files: list[BrowserEntryView]
    total_entries: int
    total_pages: int
    previous_url: str | None
    next_url: str | None
    error: ServiceErrorView | None


class SnapshotPageItemView(TypedDict, total=False):
    id: object
    short_id: str
    detail_url: str
    restore_url: str
    browse: dict[str, str]
    summary: object


class RepositoriesPageView(TypedDict):
    repositories: list[dict[str, object]]
    config_error: str | None


class RepositoryOverviewView(TypedDict):
    repositories: list[dict[str, object]]
    error: str | None


class LocationSnapshotsPageView(TypedDict):
    location_id: str
    repository: object
    label: str
    is_active: bool
    config_refs: list[dict[str, str]]
    refresh_url: str | None
    snapshots: list[SnapshotPageItemView]
    cache_empty: bool
    growth_data: dict[str, object]


class LocationSnapshotView(TypedDict):
    location_id: str
    repository: object
    label: str
    is_active: bool
    needs_adhoc_credentials: bool
    snapshot_id: str
    snapshot: dict[str, object] | None
    browse_url: str


class RepositoryBrowserService:

    def __init__(
        self,
        config_service: ConfigService,
        backup_artifact_store: RepositoryArtifactStore | None = None,
        backup_stats_service: BackupStatsService | None = None,
    ) -> None:
        self._config_service = config_service
        self._backup_artifact_store = backup_artifact_store
        self._backup_stats_service = backup_stats_service
        self._browse_cache: dict[_BrowseCacheKey, tuple[float, ResticLsResult]] = {}
        self._browse_index_groups: dict[str, BrowseIndexGroup] = {}
        self._browse_index_sequence = 0

    async def get_repositories_view(self) -> RepositoriesPageView:
        return await asyncio.to_thread(self._get_repositories_view_sync)

    def _get_repositories_view_sync(self) -> RepositoriesPageView:
        try:
            view = self.repository_overview_view()
        except ConfigServiceError as exc:
            return {"repositories": [], "config_error": exc.message}
        return {"repositories": view["repositories"], "config_error": view.get("error")}

    def _backup_growth_data(self, job: str, backup_name: str) -> dict[str, object]:
        if self._backup_stats_service is None:
            return {"has_data": False, "labels": [], "data": [], "formatted": []}
        return self._backup_stats_service.get_backup_growth_data(job, backup_name)

    async def get_location_snapshots_view(self, location_id: str) -> LocationSnapshotsPageView:
        return await asyncio.to_thread(self._get_location_snapshots_view_sync, location_id)

    def _get_location_snapshots_view_sync(self, location_id: str) -> LocationSnapshotsPageView:
        if self._backup_artifact_store is None:
            raise NotFoundServiceError(f"Location not found: {location_id}")
        location = self._backup_artifact_store.find_location(location_id)
        if location is None:
            raise NotFoundServiceError(f"Location not found: {location_id}")

        repository_id = str(location["repository_id"])
        artifacts = self._backup_artifact_store.list_present_artifacts(
            repository_id, location_id=location_id
        )
        snapshots = [artifact_snapshot_view(artifact) for artifact in artifacts]
        configs = sorted(self._configs_for_location(location_id))
        primary_config = configs[0] if configs else None
        growth_data = (
            self._backup_growth_data(primary_config[0], primary_config[1])
            if primary_config is not None
            else {"has_data": False, "labels": [], "data": [], "formatted": []}
        )
        return {
            "location_id": location_id,
            "repository": location["display_repository"],
            "label": ", ".join(f"{job}.{backup}" for job, backup in configs) or "Inactive",
            "is_active": bool(configs),
            "config_refs": [{"job": job, "backup": backup} for job, backup in configs],
            "refresh_url": f"/repositories/locations/{location_id}/refresh" if configs else None,
            "snapshots": _location_snapshot_items(location_id, snapshots),
            "cache_empty": False,
            "growth_data": growth_data,
        }

    def _configs_for_location(self, location_id: str) -> list[tuple[str, str]]:
        config = self._config_service.load_active_config()
        return self._locations_by_config(config).get(location_id, [])

    async def refresh_targets_for_location(self, location_id: str) -> list[tuple[str, str]]:
        return await asyncio.to_thread(self._refresh_targets_for_location_sync, location_id)

    def _refresh_targets_for_location_sync(self, location_id: str) -> list[tuple[str, str]]:
        if self._backup_artifact_store is None:
            raise NotFoundServiceError(f"Location not found: {location_id}")
        if self._backup_artifact_store.find_location(location_id) is None:
            raise NotFoundServiceError(f"Location not found: {location_id}")
        targets = sorted(self._configs_for_location(location_id))
        if not targets:
            raise ServiceError(
                "repository_location_inactive",
                "Repository location has no active config to refresh",
                409,
            )
        return targets

    async def get_location_snapshot_view(
        self, location_id: str, snapshot_id: str
    ) -> LocationSnapshotView:
        return await asyncio.to_thread(
            self._get_location_snapshot_view_sync, location_id, snapshot_id
        )

    def _get_location_snapshot_view_sync(
        self, location_id: str, snapshot_id: str
    ) -> LocationSnapshotView:
        if self._backup_artifact_store is None:
            raise NotFoundServiceError(f"Location not found: {location_id}")
        location = self._backup_artifact_store.find_location(location_id)
        if location is None:
            raise NotFoundServiceError(f"Location not found: {location_id}")

        snapshot_id = _validate_snapshot_id(snapshot_id)
        raw_snapshots = self._backup_artifact_store.list_present_artifacts(
            str(location["repository_id"]), location_id=location_id
        )
        snapshot = _find_snapshot_metadata(raw_snapshots, snapshot_id)
        configs = self._configs_for_location(location_id)
        return {
            "location_id": location_id,
            "repository": location["display_repository"],
            "label": ", ".join(f"{job}.{backup}" for job, backup in configs) or "Inactive",
            "is_active": bool(configs),
            "needs_adhoc_credentials": self.resolve_backup_config_for_location(location_id) is None,
            "snapshot_id": snapshot_id,
            "snapshot": snapshot,
            "browse_url": _location_browse_url(location_id, snapshot_id, "/"),
        }

    async def browse_location_snapshot_view(
        self, *, location_id: str, snapshot_id: str, path: str = "/", page: int = 1
    ) -> LocationBrowseView:
        snapshot_id = _validate_snapshot_id(snapshot_id)
        normalized_path = _validate_snapshot_path(path)
        page = _validate_page(page)

        resolved = await asyncio.to_thread(self.resolve_backup_config_for_location, location_id)
        base_view = {
            "location_id": location_id,
            "snapshot_id": snapshot_id,
            "path": normalized_path,
            "page": page,
            "page_size": BROWSE_PAGE_SIZE,
            "parent_path": _parent_path(normalized_path),
            "breadcrumbs": _location_breadcrumbs(location_id, snapshot_id, normalized_path),
            "prefetch_url": _location_prefetch_url(location_id, snapshot_id, normalized_path, page),
            "indexed_paths": self._browse_index_cache_path_count(),
            "indexed_bytes": self._browse_index_cache_size(),
            "indexed_bytes_limit": BROWSE_INDEX_CACHE_MAX_BYTES_TOTAL,
        }
        if resolved is None:
            return cast(
                LocationBrowseView,
                {
                    **base_view,
                    "ok": False,
                    "entries": [],
                    "directories": [],
                    "files": [],
                    "total_entries": 0,
                    "total_pages": 0,
                    "previous_url": None,
                    "next_url": None,
                    "error": _error(
                        "no_credentials",
                        "No configured backup currently provides credentials for this location",
                    ),
                },
            )

        job_name, backup_name, backup_config = resolved
        result = await self._cached_restic_ls(
            backup_config, snapshot_id, normalized_path, job_name, backup_name
        )
        if not result.ok:
            return cast(
                LocationBrowseView,
                {
                    **base_view,
                    "ok": False,
                    "entries": [],
                    "directories": [],
                    "files": [],
                    "total_entries": 0,
                    "total_pages": 0,
                    "previous_url": None,
                    "next_url": None,
                    "error": result.error,
                },
            )

        all_entries = result.entries
        total_entries = len(all_entries)
        total_pages = max(1, (total_entries + BROWSE_PAGE_SIZE - 1) // BROWSE_PAGE_SIZE)
        if page > total_pages:
            raise ServiceError("invalid_parameter", f"Invalid browse page: {page}")
        page_start = (page - 1) * BROWSE_PAGE_SIZE
        entries = [
            cast(
                BrowserEntryView,
                {
                    **entry,
                    "browse_url": (
                        _location_browse_url(location_id, snapshot_id, str(entry["path"]))
                        if entry["type"] == "dir"
                        else None
                    ),
                },
            )
            for entry in all_entries[page_start : page_start + BROWSE_PAGE_SIZE]
        ]
        directories = [entry for entry in entries if entry["type"] == "dir"]
        files = [entry for entry in entries if entry["type"] != "dir"]
        return cast(
            LocationBrowseView,
            {
                **base_view,
                "ok": True,
                "entries": entries,
                "directories": directories,
                "files": files,
                "total_entries": total_entries,
                "total_pages": total_pages,
                "previous_url": (
                    _location_browse_url(location_id, snapshot_id, normalized_path, page - 1)
                    if page > 1
                    else None
                ),
                "next_url": (
                    _location_browse_url(location_id, snapshot_id, normalized_path, page + 1)
                    if page < total_pages
                    else None
                ),
                "error": None,
            },
        )

    async def prefetch_location_snapshot_view(
        self, *, location_id: str, snapshot_id: str, path: str = "/", page: int = 1
    ) -> LocationBrowseView:
        """Recursively prefetch one snapshot subtree into the process-local browse cache."""
        snapshot_id = _validate_snapshot_id(snapshot_id)
        normalized_path = _validate_snapshot_path(path)
        page = _validate_page(page)
        resolved = await asyncio.to_thread(self.resolve_backup_config_for_location, location_id)
        base_view = {
            "location_id": location_id,
            "snapshot_id": snapshot_id,
            "path": normalized_path,
            "page": page,
            "page_size": BROWSE_PAGE_SIZE,
            "parent_path": _parent_path(normalized_path),
            "breadcrumbs": _location_breadcrumbs(location_id, snapshot_id, normalized_path),
            "prefetch_url": _location_prefetch_url(location_id, snapshot_id, normalized_path, page),
            "indexed_paths": self._browse_index_cache_path_count(),
            "indexed_bytes": self._browse_index_cache_size(),
            "indexed_bytes_limit": BROWSE_INDEX_CACHE_MAX_BYTES_TOTAL,
        }

        if resolved is None:
            return await self.browse_location_snapshot_view(
                location_id=location_id,
                snapshot_id=snapshot_id,
                path=normalized_path,
                page=page,
            )

        job_name, backup_name, backup_config = resolved
        result = await _run_restic_ls_recursive(
            backup_config, snapshot_id, normalized_path, job_name, backup_name
        )
        if not result.ok:
            return cast(
                LocationBrowseView,
                {
                    **base_view,
                    "ok": False,
                    "entries": [],
                    "directories": [],
                    "files": [],
                    "total_entries": 0,
                    "total_pages": 0,
                    "previous_url": None,
                    "next_url": None,
                    "error": result.error,
                },
            )
        self._remember_recursive_browse_result(backup_config, snapshot_id, normalized_path, result)
        return await self.browse_location_snapshot_view(
            location_id=location_id,
            snapshot_id=snapshot_id,
            path=normalized_path,
            page=page,
        )

    async def _cached_restic_ls(
        self,
        backup_config: ResolvedBackupConfig,
        snapshot_id: str,
        normalized_path: str,
        job_name: str,
        backup_name: str,
    ) -> ResticLsResult:
        cache_key = _browse_cache_key(backup_config, snapshot_id, normalized_path)
        cached = self._get_browse_cache(cache_key)
        if cached is not None:
            return cached

        result = await _run_restic_ls(
            backup_config, snapshot_id, normalized_path, job_name, backup_name
        )
        if result.ok:
            self._remember_browse_result(cache_key, result)
        return result

    def _get_browse_cache(self, cache_key: _BrowseCacheKey) -> ResticLsResult | None:
        cached = self._browse_cache.get(cache_key)
        if cached is None:
            return None
        cached_at, result = cached
        if time.monotonic() - cached_at > BROWSE_CACHE_TTL_SECONDS:
            group_ids = self._index_group_ids_for_cache_key(cache_key)
            if group_ids:
                for group_id in group_ids:
                    self._remove_index_group(group_id)
            else:
                self._browse_cache.pop(cache_key, None)
            return None
        return _copy_restic_ls_result(result)

    def _remember_browse_result(self, cache_key: _BrowseCacheKey, result: ResticLsResult) -> None:
        self._browse_cache[cache_key] = (time.monotonic(), _copy_restic_ls_result(result))

    def _remember_recursive_browse_result(
        self,
        backup_config: ResolvedBackupConfig,
        snapshot_id: str,
        root_path: str,
        result: ResticRecursiveLsResult,
    ) -> None:
        self._remove_replaced_index_groups(backup_config.repository, snapshot_id)
        cache_keys: set[_BrowseCacheKey] = set()
        estimated_bytes = 0
        now = time.monotonic()
        for directory, entries in result.directories.items():
            cache_key = _browse_cache_key(backup_config, snapshot_id, directory)
            cached_result = ResticLsResult(True, entries)
            self._browse_cache[cache_key] = (now, _copy_restic_ls_result(cached_result))
            cache_keys.add(cache_key)
            estimated_bytes += _estimate_restic_ls_result_size(cached_result)

        group_id = self._next_index_group_id()
        self._browse_index_groups[group_id] = BrowseIndexGroup(
            group_id=group_id,
            repository=backup_config.repository,
            snapshot_id=snapshot_id,
            root_path=root_path,
            created_at=now,
            estimated_bytes=estimated_bytes,
            cache_keys=cache_keys,
        )
        self._prune_index_groups(keep_group_id=group_id)

    def _next_index_group_id(self) -> str:
        self._browse_index_sequence += 1
        return f"index-{self._browse_index_sequence}"

    def _browse_index_cache_path_count(self) -> int:
        self._drop_expired_browse_cache_entries()
        return sum(len(group.cache_keys) for group in self._browse_index_groups.values())

    def _browse_index_cache_size(self) -> int:
        self._drop_expired_browse_cache_entries()
        return sum(group.estimated_bytes for group in self._browse_index_groups.values())

    def _prune_index_groups(self, *, keep_group_id: str) -> None:
        self._drop_expired_browse_cache_entries()
        while self._browse_index_cache_size() > BROWSE_INDEX_CACHE_MAX_BYTES_TOTAL:
            candidates = [
                group
                for group in self._browse_index_groups.values()
                if group.group_id != keep_group_id
            ]
            if not candidates:
                return
            oldest = min(candidates, key=lambda group: group.created_at)
            self._remove_index_group(oldest.group_id)

    def _remove_replaced_index_groups(self, repository: str, snapshot_id: str) -> None:
        replaced = [
            group.group_id
            for group in self._browse_index_groups.values()
            if group.repository == repository and group.snapshot_id == snapshot_id
        ]
        for group_id in replaced:
            self._remove_index_group(group_id)

    def _remove_index_group(self, group_id: str) -> None:
        group = self._browse_index_groups.pop(group_id, None)
        if group is None:
            return
        remaining_group_keys = set().union(
            *(other.cache_keys for other in self._browse_index_groups.values())
        )
        for cache_key in group.cache_keys:
            if cache_key not in remaining_group_keys:
                self._browse_cache.pop(cache_key, None)

    def _index_group_ids_for_cache_key(self, cache_key: _BrowseCacheKey) -> list[str]:
        return [
            group.group_id
            for group in self._browse_index_groups.values()
            if cache_key in group.cache_keys
        ]

    def _index_group_cache_keys(self) -> set[_BrowseCacheKey]:
        if not self._browse_index_groups:
            return set()
        return set().union(*(group.cache_keys for group in self._browse_index_groups.values()))

    def _drop_expired_browse_cache_entries(self) -> None:
        now = time.monotonic()
        expired_group_ids = [
            group.group_id
            for group in self._browse_index_groups.values()
            if now - group.created_at > BROWSE_CACHE_TTL_SECONDS
        ]
        for group_id in expired_group_ids:
            self._remove_index_group(group_id)
        grouped_keys = self._index_group_cache_keys()
        expired_keys = [
            cache_key
            for cache_key, (cached_at, _result) in self._browse_cache.items()
            if cache_key not in grouped_keys and now - cached_at > BROWSE_CACHE_TTL_SECONDS
        ]
        for cache_key in expired_keys:
            self._browse_cache.pop(cache_key, None)

    def repository_overview_view(self) -> RepositoryOverviewView:
        config = self._config_service.load_active_config()
        if self._backup_artifact_store is None:
            return {"repositories": [], "error": None}

        repositories = self._backup_artifact_store.list_repositories()
        locations_by_config = self._locations_by_config(config)

        repository_views = [
            self._repository_view(repository, locations_by_config) for repository in repositories
        ]
        repository_views.sort(key=lambda repository: cast(str, repository["title"]))

        return {"repositories": repository_views, "error": None}

    def _repository_view(
        self,
        repository: dict[str, object],
        locations_by_config: dict[str, list[tuple[str, str]]],
    ) -> dict[str, object]:
        locations = cast(list[dict[str, object]], repository.get("locations", []))
        title = " / ".join(
            dict.fromkeys(Path(str(location["display_repository"])).name for location in locations)
        )
        enriched = [
            {
                **location,
                "repository_id": repository["repository_id"],
                "delete_url": f"/repositories/locations/{location['location_id']}/delete",
                "merge_url": f"/repositories/locations/{location['location_id']}/merge",
                "view_url": f"/repositories/locations/{location['location_id']}/snapshots",
            }
            for location in locations
        ]
        for location in enriched:
            configs = locations_by_config.get(str(location["location_id"]), [])
            location["label"] = (
                ", ".join(f"{job}.{backup}" for job, backup in configs) or "Inactive"
            )
            location["config_refs"] = [
                {"job": job, "backup": backup} for job, backup in sorted(configs)
            ]
            location["is_active"] = bool(configs)
            location["merge_targets"] = [
                {
                    "location_id": target["location_id"],
                    "display_repository": target["display_repository"],
                }
                for target in enriched
                if target["location_id"] != location["location_id"]
            ]
        return {
            "title": title,
            "repository_id": repository["repository_id"],
            "backend_repository_id": repository.get("backend_repository_id"),
            "locations": enriched,
        }

    def _locations_by_config(self, config: ResolvedAppConfig) -> dict[str, list[tuple[str, str]]]:
        locations_by_config: dict[str, list[tuple[str, str]]] = {}
        for job_name, backup_name, resolution in self._resolved_backup_configs(config):
            location_id = str(resolution["location_id"])
            locations_by_config.setdefault(location_id, []).append((job_name, backup_name))
        return locations_by_config

    def _configs_by_repository(self, config: ResolvedAppConfig) -> dict[str, list[tuple[str, str]]]:
        configs_by_repository: dict[str, list[tuple[str, str]]] = {}
        for job_name, backup_name, resolution in self._resolved_backup_configs(config):
            repository_id = str(resolution["repository_id"])
            configs_by_repository.setdefault(repository_id, []).append((job_name, backup_name))
        return configs_by_repository

    def _resolved_backup_configs(
        self, config: ResolvedAppConfig
    ) -> Iterator[tuple[str, str, dict[str, object]]]:
        """Yield ``(job_name, backup_name, location_resolution)`` for every known config."""
        if self._backup_artifact_store is None:
            return
        for job_name, job in config.jobs.items():
            for backup_name, backup in job.backup.items():
                location_key = repository_location_key_for_display(backup.repository)
                resolution = self._backup_artifact_store.resolve_location(location_key)
                if resolution is None:
                    continue
                yield job_name, backup_name, resolution

    def resolve_backup_config_for_location(
        self, location_id: str
    ) -> tuple[str, str, ResolvedBackupConfig] | None:
        """Resolve credentials for one location.

        Uses the location's own active config if any, otherwise borrows
        credentials from a donor config that currently points anywhere
        within the same physical repository identity.

        Returns:
            ``(job_name, backup_name, config)`` or ``None`` if no config
            exists anywhere for this repository.
        """
        if self._backup_artifact_store is None:
            return None
        location = self._backup_artifact_store.find_location(location_id)
        if location is None:
            return None

        config = self._config_service.load_active_config()

        active_configs = self._locations_by_config(config).get(location_id, [])
        if active_configs:
            job_name, backup_name = sorted(active_configs)[0]
            _backup_name, backup_config = self._backup(job_name, backup_name)
            return job_name, backup_name, backup_config

        repository_id = str(location["repository_id"])
        donor_configs = self._configs_by_repository(config).get(repository_id, [])
        if not donor_configs:
            return None
        donor_job, donor_backup = sorted(donor_configs)[0]
        _donor_backup_name, donor_config = self._backup(donor_job, donor_backup)
        copied_config = donor_config.model_copy(
            update={"repository": str(location["display_repository"])}
        )
        return donor_job, donor_backup, copied_config

    def build_adhoc_backup_config(
        self,
        location_id: str,
        *,
        password: str | None,
        password_env: str | None,
        password_file: str | None,
    ) -> ResolvedBackupConfig:
        """Build a transient, never-persisted config from user-supplied credentials.

        Used as a last resort when no active config and no donor config exist
        anywhere for a location's repository identity. Exactly one of
        ``password``/``password_env``/``password_file`` must be given (already
        normalized: blank/whitespace-only values must be ``None`` by the
        caller). The result is built fresh on every call and is never written
        to the store or config.
        """
        if self._backup_artifact_store is None:
            raise NotFoundServiceError(f"Location not found: {location_id}")
        location = self._backup_artifact_store.find_location(location_id)
        if location is None:
            raise NotFoundServiceError(f"Location not found: {location_id}")

        given = [value for value in (password, password_env, password_file) if value is not None]
        if not given:
            raise ServiceError(
                "invalid_parameter",
                "One of password, password_env, or password_file is required",
                400,
            )
        if len(given) > 1:
            raise ServiceError(
                "invalid_parameter",
                "password, password_env, and password_file are mutually exclusive",
                400,
            )

        resolved_password: str | None = None
        if password is not None:
            resolved_password = password
        elif password_env is not None:
            resolved_password = os.environ.get(password_env)
            if resolved_password is None or resolved_password == "":
                raise ServiceError(
                    "invalid_parameter",
                    f"Environment variable {password_env} is not set or is empty",
                    400,
                )
        elif password_file is not None:
            file_path = Path(password_file)
            if not file_path.is_absolute():
                raise ServiceError(
                    "invalid_parameter",
                    f"password_file {password_file!r} must be an absolute path",
                    400,
                )
            if not file_path.is_file():
                raise ServiceError(
                    "invalid_parameter",
                    f"password_file {password_file!r} does not exist or is not a file",
                    400,
                )

        return ResolvedBackupConfig(
            repository=str(location["display_repository"]),
            credentials=ResolvedCredentials(
                password=resolved_password,
                password_env=password_env,
                password_file=password_file,
            ),
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


async def _run_restic_ls(
    backup: ResolvedBackupConfig,
    snapshot_id: str,
    snapshot_path: str,
    job_name: str,
    backup_name: str,
) -> ResticLsResult:
    cmd = ["restic", "--repo", backup.repository, "ls", "--json", snapshot_id, snapshot_path]
    parser = _ResticLsStreamParser(snapshot_path)
    timeout = env_timeout(BROWSE_TIMEOUT_ENV, DEFAULT_BROWSE_TIMEOUT, logger)
    try:
        result = await stream_command(
            cmd,
            on_stdout=parser.feed_stdout,
            on_stderr=parser.feed_stderr,
            env=resolve_env(backup.credentials.password, backup.credentials.password_file),
            timeout=timeout,
            read_size=_STREAM_READ_SIZE,
        )
    except asyncio.TimeoutError:
        output, truncated = limit_output(
            f"Command '{cmd}' timed out after {timeout} seconds. "
            f"Adjust {BROWSE_TIMEOUT_ENV} to change this timeout."
        )
        return ResticLsResult(
            False,
            [],
            _error("timeout", f"restic ls timed out: {output}", truncated),
        )
    except OSError as exc:
        output, truncated = limit_output(str(exc))
        return ResticLsResult(
            False,
            [],
            _error("os_error", f"restic ls failed: {output}", truncated),
        )

    if result.returncode != 0:
        raw_output = parser.output_sample()
        output, truncated = limit_output(raw_output)
        return ResticLsResult(
            False,
            [],
            _error(
                "restic_error",
                f"restic ls exited with {result.returncode}: {output or 'no output'}",
                truncated,
            ),
        )

    return parser.finish()


async def _run_restic_ls_recursive(
    backup: ResolvedBackupConfig,
    snapshot_id: str,
    snapshot_path: str,
    job_name: str,
    backup_name: str,
) -> ResticRecursiveLsResult:
    cmd = [
        "restic",
        "--repo",
        backup.repository,
        "ls",
        "--json",
        "--recursive",
        snapshot_id,
        snapshot_path,
    ]
    parser = _ResticRecursiveLsStreamParser(snapshot_path)
    timeout = env_timeout(BROWSE_TIMEOUT_ENV, DEFAULT_BROWSE_TIMEOUT, logger)
    try:
        result = await stream_command(
            cmd,
            on_stdout=parser.feed_stdout,
            on_stderr=parser.feed_stderr,
            env=resolve_env(backup.credentials.password, backup.credentials.password_file),
            timeout=timeout,
            read_size=_STREAM_READ_SIZE,
        )
    except asyncio.TimeoutError:
        output, truncated = limit_output(
            f"Command '{cmd}' timed out after {timeout} seconds. "
            f"Adjust {BROWSE_TIMEOUT_ENV} to change this timeout."
        )
        return ResticRecursiveLsResult(
            False,
            {},
            _error("timeout", f"restic recursive ls timed out: {output}", truncated),
        )
    except OSError as exc:
        output, truncated = limit_output(str(exc))
        return ResticRecursiveLsResult(
            False,
            {},
            _error("os_error", f"restic recursive ls failed: {output}", truncated),
        )

    if result.returncode != 0:
        raw_output = parser.output_sample()
        output, truncated = limit_output(raw_output)
        return ResticRecursiveLsResult(
            False,
            {},
            _error(
                "restic_error",
                f"restic recursive ls exited with {result.returncode}: {output or 'no output'}",
                truncated,
            ),
        )

    return parser.finish()


class _ResticLsStreamParser:
    """Incrementally parse ``restic ls --json`` while keeping only direct children."""

    def __init__(self, base_path: str) -> None:
        self._base_path = base_path
        self._children: dict[str, BrowserEntryView] = {}
        self._line_buffer = b""
        self._stdout_sample = bytearray()
        self._stderr_sample = bytearray()
        self._saw_listing_object = False
        self._error: ServiceErrorView | None = None

    @property
    def error(self) -> ServiceErrorView | None:
        return self._error

    def feed_stdout(self, chunk: bytes) -> None:
        """Feed stdout bytes from restic."""
        self._append_sample(self._stdout_sample, chunk)
        if self._error is not None:
            return

        self._line_buffer += chunk
        while b"\n" in self._line_buffer:
            line, self._line_buffer = self._line_buffer.split(b"\n", 1)
            self._parse_json_line(line)

    def feed_stderr(self, chunk: bytes) -> None:
        """Feed stderr bytes from restic for bounded diagnostics."""
        self._append_sample(self._stderr_sample, chunk)

    def finish(self) -> ResticLsResult:
        if self._error is not None:
            return ResticLsResult(False, [], self._error)
        if self._line_buffer.strip():
            self._parse_json_line(self._line_buffer)
        if self._error is not None:
            return ResticLsResult(False, [], self._error)
        if not self._saw_listing_object:
            if self._stdout_sample.strip():
                return ResticLsResult(
                    False, [], _error("unexpected_response", "unexpected restic ls response")
                )
            return ResticLsResult(False, [], _error("empty_response", "empty restic ls response"))
        entries = sorted(
            self._children.values(), key=lambda entry: (entry["type"] != "dir", str(entry["name"]))
        )
        return ResticLsResult(True, entries)

    def output_sample(self) -> str:
        raw = self._stderr_sample + self._stdout_sample
        return raw.decode(errors="replace").strip()

    def _parse_json_line(self, line: bytes) -> None:
        text = line.strip()
        if not text:
            return
        try:
            item = json.loads(text.decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            output_text, truncated = limit_output(str(exc))
            self._error = _error(
                "invalid_json", f"invalid restic ls JSON output: {output_text}", truncated
            )
            return
        self._handle_item(item)

    def _handle_item(self, item: object) -> None:
        if not isinstance(item, dict):
            self._error = _error("unexpected_response", "unexpected restic ls response")
            return
        item_kind = item.get("message_type", item.get("struct_type"))
        item_type = item.get("type")
        item_path = item.get("path")
        if item_kind == "snapshot":
            self._saw_listing_object = True
            return
        if item_kind == "node" and isinstance(item_type, str) and isinstance(item_path, str):
            self._saw_listing_object = True
            self._add_direct_child(cast(dict[str, Any], item), item_path, item_type)
            return
        self._error = _error("unexpected_response", "unexpected restic ls response")

    def _add_direct_child(self, raw_entry: dict[str, Any], raw_path: str, raw_type: str) -> None:
        if raw_path == self._base_path:
            return
        try:
            normalized_entry_path = _validate_snapshot_path(raw_path)
        except ServiceError:
            return
        child_name = _child_name(self._base_path, normalized_entry_path)
        if child_name is None:
            return
        child_path = _join_snapshot_path(self._base_path, child_name)
        child_type = (
            "dir" if "/" in _relative_path(self._base_path, normalized_entry_path) else raw_type
        )
        child_type = "dir" if child_type == "dir" else "file"
        existing = self._children.get(child_path)
        if existing is not None and existing["type"] == "dir":
            return
        self._children[child_path] = {
            "name": child_name,
            "path": child_path,
            "type": child_type,
            "size": raw_entry.get("size") if child_type != "dir" else None,
            "mode": raw_entry.get("mode") if raw_entry.get("mode") else None,
            "mtime": raw_entry.get("mtime") if raw_entry.get("mtime") else None,
            "browse": (
                {
                    "path": child_path,
                }
                if child_type == "dir"
                else None
            ),
            "browse_url": None,
        }

    @staticmethod
    def _append_sample(sample: bytearray, chunk: bytes) -> None:
        remaining = _STREAM_SAMPLE_LIMIT - len(sample)
        if remaining > 0:
            sample.extend(chunk[:remaining])


class _ResticRecursiveLsStreamParser:
    """Parse recursive ``restic ls --json`` into cached direct-child listings."""

    def __init__(self, base_path: str) -> None:
        self._base_path = base_path
        self._directories: dict[str, dict[str, BrowserEntryView]] = {base_path: {}}
        self._line_buffer = b""
        self._stdout_sample = bytearray()
        self._stderr_sample = bytearray()
        self._saw_listing_object = False
        self._error: ServiceErrorView | None = None

    def feed_stdout(self, chunk: bytes) -> None:
        """Feed stdout bytes from restic."""
        self._append_sample(self._stdout_sample, chunk)
        if self._error is not None:
            return

        self._line_buffer += chunk
        while b"\n" in self._line_buffer:
            line, self._line_buffer = self._line_buffer.split(b"\n", 1)
            self._parse_json_line(line)

    def feed_stderr(self, chunk: bytes) -> None:
        """Feed stderr bytes from restic for bounded diagnostics."""
        self._append_sample(self._stderr_sample, chunk)

    def finish(self) -> ResticRecursiveLsResult:
        if self._error is not None:
            return ResticRecursiveLsResult(False, {}, self._error)
        if self._line_buffer.strip():
            self._parse_json_line(self._line_buffer)
        if self._error is not None:
            return ResticRecursiveLsResult(False, {}, self._error)
        if not self._saw_listing_object:
            if self._stdout_sample.strip():
                return ResticRecursiveLsResult(
                    False, {}, _error("unexpected_response", "unexpected restic ls response")
                )
            return ResticRecursiveLsResult(
                False, {}, _error("empty_response", "empty restic ls response")
            )
        directories = {
            directory: sorted(
                children.values(),
                key=lambda entry: (entry["type"] != "dir", str(entry["name"])),
            )
            for directory, children in self._directories.items()
        }
        return ResticRecursiveLsResult(True, directories)

    def output_sample(self) -> str:
        raw = self._stderr_sample + self._stdout_sample
        return raw.decode(errors="replace").strip()

    def _parse_json_line(self, line: bytes) -> None:
        text = line.strip()
        if not text:
            return
        try:
            item = json.loads(text.decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            output_text, truncated = limit_output(str(exc))
            self._error = _error(
                "invalid_json", f"invalid restic ls JSON output: {output_text}", truncated
            )
            return
        self._handle_item(item)

    def _handle_item(self, item: object) -> None:
        if not isinstance(item, dict):
            self._error = _error("unexpected_response", "unexpected restic ls response")
            return
        item_kind = item.get("message_type", item.get("struct_type"))
        item_type = item.get("type")
        item_path = item.get("path")
        if item_kind == "snapshot":
            self._saw_listing_object = True
            return
        if item_kind == "node" and isinstance(item_type, str) and isinstance(item_path, str):
            self._saw_listing_object = True
            self._add_descendant(cast(dict[str, Any], item), item_path, item_type)
            return
        self._error = _error("unexpected_response", "unexpected restic ls response")

    def _add_descendant(self, raw_entry: dict[str, Any], raw_path: str, raw_type: str) -> None:
        if raw_path == self._base_path:
            return
        try:
            normalized_path = _validate_snapshot_path(raw_path)
        except ServiceError:
            return
        relative_path = _relative_path(self._base_path, normalized_path)
        if (
            not relative_path
            or relative_path.startswith("/")
            or relative_path == ".."
            or relative_path.startswith("../")
        ):
            return
        parent_path = self._base_path
        parts = relative_path.split("/")
        for index, name in enumerate(parts):
            child_path = _join_snapshot_path(parent_path, name)
            is_leaf = index == len(parts) - 1
            child_type = raw_type if is_leaf else "dir"
            child_type = "dir" if child_type == "dir" else "file"
            self._add_child(parent_path, name, child_path, child_type, raw_entry if is_leaf else {})
            if child_type != "dir":
                return
            parent_path = child_path
            self._directories.setdefault(parent_path, {})

    def _add_child(
        self,
        parent_path: str,
        child_name: str,
        child_path: str,
        child_type: str,
        raw_entry: dict[str, Any],
    ) -> None:
        children = self._directories.setdefault(parent_path, {})
        existing = children.get(child_path)
        if existing is not None and existing["type"] == "dir":
            return
        children[child_path] = {
            "name": child_name,
            "path": child_path,
            "type": child_type,
            "size": raw_entry.get("size") if child_type != "dir" else None,
            "mode": raw_entry.get("mode") if raw_entry.get("mode") else None,
            "mtime": raw_entry.get("mtime") if raw_entry.get("mtime") else None,
            "browse": {"path": child_path} if child_type == "dir" else None,
            "browse_url": None,
        }

    @staticmethod
    def _append_sample(sample: bytearray, chunk: bytes) -> None:
        remaining = _STREAM_SAMPLE_LIMIT - len(sample)
        if remaining > 0:
            sample.extend(chunk[:remaining])


def _validate_name(value: str, kind: str) -> str:
    if not _NAME_RE.fullmatch(value):
        raise ServiceError("invalid_parameter", f"Invalid {kind} name: {value!r}")
    return value


def _browse_cache_key(
    backup_config: ResolvedBackupConfig, snapshot_id: str, normalized_path: str
) -> _BrowseCacheKey:
    return (backup_config.repository, snapshot_id, normalized_path)


def _copy_restic_ls_result(result: ResticLsResult) -> ResticLsResult:
    return ResticLsResult(
        ok=result.ok,
        entries=[cast(BrowserEntryView, dict(entry)) for entry in result.entries],
        error=cast(ServiceErrorView, dict(result.error)) if result.error is not None else None,
    )


def _estimate_restic_ls_result_size(result: ResticLsResult) -> int:
    size = _BROWSE_CACHE_ENTRY_OVERHEAD_BYTES
    for entry in result.entries:
        size += _BROWSE_CACHE_ENTRY_OVERHEAD_BYTES
        for key in ("name", "path", "type", "mode", "mtime"):
            value = entry.get(key)
            if value is not None:
                size += len(str(value).encode())
        browse = entry.get("browse")
        if isinstance(browse, dict):
            size += _BROWSE_CACHE_ENTRY_OVERHEAD_BYTES
            for value in browse.values():
                size += len(str(value).encode())
        browse_url = entry.get("browse_url")
        if browse_url is not None:
            size += len(str(browse_url).encode())
        size += 16
    if result.error is not None:
        size += _BROWSE_CACHE_ENTRY_OVERHEAD_BYTES
        size += sum(len(str(value).encode()) for value in result.error.values())
    return size


def _validate_snapshot_id(value: str) -> str:
    if not _SNAPSHOT_ID_RE.fullmatch(value):
        raise ServiceError(
            "invalid_parameter",
            "Invalid snapshot id: expected 8 to 64 hexadecimal characters",
        )
    return value


def _validate_snapshot_path(value: str | None) -> str:
    if value is None or value == "":
        return "/"
    if any(char in value for char in ("\x00", "\\", "\r", "\n")):
        raise ServiceError("invalid_parameter", "Invalid snapshot path")
    if not value.startswith("/"):
        raise ServiceError("invalid_parameter", "Snapshot path must be absolute")
    normalized = value.rstrip("/") or "/"
    if normalized == "/":
        return normalized
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts[1:]):
        raise ServiceError("invalid_parameter", "Invalid snapshot path")
    return normalized


def _validate_page(value: int) -> int:
    if value < 1:
        raise ServiceError("invalid_parameter", f"Invalid browse page: {value}")
    return value


def _parent_path(value: str) -> str | None:
    if value == "/":
        return None
    parent = value.rsplit("/", 1)[0]
    return parent or "/"


def _location_breadcrumbs(location_id: str, snapshot_id: str, value: str) -> list[BreadcrumbView]:
    breadcrumbs: list[BreadcrumbView] = [
        {
            "name": "/",
            "path": "/",
            "browse_url": _location_browse_url(location_id, snapshot_id, "/"),
        }
    ]
    if value == "/":
        return breadcrumbs
    current = ""
    for part in value.strip("/").split("/"):
        current = f"{current}/{part}"
        breadcrumbs.append(
            {
                "name": part,
                "path": current,
                "browse_url": _location_browse_url(location_id, snapshot_id, current),
            }
        )
    return breadcrumbs


def _location_snapshot_items(
    location_id: str, snapshots: list[dict[str, Any]]
) -> list[SnapshotPageItemView]:
    items: list[SnapshotPageItemView] = []
    for snapshot in snapshots:
        snapshot_id = str(snapshot.get("id", ""))
        if not _SNAPSHOT_ID_RE.fullmatch(snapshot_id):
            continue
        summary = snapshot.get("summary")
        if isinstance(summary, dict) and "total_duration" not in summary:
            duration = compute_duration(summary)
            if duration is not None:
                snapshot = {**snapshot, "summary": {**summary, "total_duration": duration}}
        short_id = snapshot_id[:8]
        items.append(
            cast(
                SnapshotPageItemView,
                {
                    **snapshot,
                    "short_id": short_id,
                    "detail_url": f"/repositories/locations/{location_id}/snapshots/{short_id}",
                    "restore_url": (
                        f"/repositories/locations/{location_id}/snapshots/{short_id}/restore"
                    ),
                },
            )
        )
    return items


def _location_browse_url(
    location_id: str, snapshot_id: str, snapshot_path: str, page: int = 1
) -> str:
    query_values: dict[str, object] = {"path": snapshot_path}
    if page > 1:
        query_values["page"] = page
    query = urlencode(query_values)
    return f"/repositories/locations/{location_id}/snapshots/{snapshot_id}/browse?{query}"


def _location_prefetch_url(
    location_id: str, snapshot_id: str, snapshot_path: str, page: int = 1
) -> str:
    query_values: dict[str, object] = {"path": snapshot_path}
    if page > 1:
        query_values["page"] = page
    query = urlencode(query_values)
    return f"/repositories/locations/{location_id}/snapshots/{snapshot_id}/prefetch?{query}"


def _find_snapshot_metadata(
    raw_snapshots: list[dict[str, object]], snapshot_id: str
) -> dict[str, object] | None:
    for artifact in raw_snapshots:
        raw_snapshot = artifact_snapshot_view(artifact)
        raw_id = str(raw_snapshot.get("id", ""))
        short_id = raw_id[:8]
        if snapshot_id in {raw_id, short_id}:
            summary = raw_snapshot.get("summary")
            if isinstance(summary, dict) and "total_duration" not in summary:
                duration = compute_duration(summary)
                if duration is not None:
                    return {
                        **raw_snapshot,
                        "short_id": short_id,
                        "summary": {**summary, "total_duration": duration},
                    }
            return {**raw_snapshot, "short_id": short_id}
    return None


def _relative_path(base_path: str, child_path: str) -> str:
    if base_path == "/":
        return child_path.strip("/")
    return child_path.removeprefix(f"{base_path}/")


def _child_name(base_path: str, child_path: str) -> str | None:
    relative = _relative_path(base_path, child_path)
    if not relative or relative.startswith("/") or relative == ".." or relative.startswith("../"):
        return None
    return relative.split("/", 1)[0]


def _join_snapshot_path(base_path: str, child_name: str) -> str:
    if base_path == "/":
        return f"/{child_name}"
    return f"{base_path}/{child_name}"


def _error(code: str, message: str, output_truncated: bool = False) -> ServiceErrorView:
    return {"code": code, "message": message, "output_truncated": output_truncated}
