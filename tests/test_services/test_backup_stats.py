import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import ParamSpec, TypeVar
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from src.services.backup_stats import BackupStatsService
from src.services.config import ConfigService

P = ParamSpec("P")
T = TypeVar("T")


def _write_stats_config(path: Path, repository: str = "/repo") -> None:
    path.write_text(
        f"""
[jobs.j.backup.p]
repository = "{repository}"
sources = ["/data"]
password = "secret-value"
""".strip(),
        encoding="utf-8",
    )


@pytest.fixture()
def run_to_thread_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def immediate(func: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", immediate)


def _make_full_service(
    tmp_path: Path,
    cached: dict[str, object] | None = None,
) -> BackupStatsService:
    config_path = tmp_path / "config.toml"
    _write_stats_config(config_path)
    store = MagicMock()
    if cached is None:
        store.resolve_location.return_value = None
    else:
        store.resolve_location.return_value = {
            "repository_id": "repo-id",
            "location_id": "loc-id",
            "display_repository": "/repo",
        }
        store.list_present_artifacts.return_value = cached.get("artifacts", [])
        store.growth_points.return_value = cached.get("points", [])
    return BackupStatsService(store, config_service=ConfigService(config_path))


def test_get_backup_stats_returns_cached_value(tmp_path: Path) -> None:
    cached = {
        "artifacts": [
            {
                "backend_artifact_id": "abcdef1234567890",
                "backend_artifact_short_id": "abcdef12",
                "created_at": "2026-06-01T12:00:00+00:00",
                "paths": ["/data"],
                "metadata": {"hostname": "host1"},
            }
        ],
        "points": [
            {
                "stats_point_id": "point-1",
                "collected_at": "2026-06-01T12:10:00+00:00",
                "total_size_bytes": 2048,
                "artifacts_count": 7,
                "raw_stats_json": '{"total_file_count": 9}',
            }
        ],
    }
    service = _make_full_service(tmp_path, cached=cached)
    result = service.get_backup_stats("j", "p")
    assert result is not None
    assert result["job"] == "j"
    assert result["backup"] == "p"
    assert result["snapshots_count"] == 7
    assert result["total_size_bytes"] == 2048
    assert result["total_file_count"] == 9
    assert result["snapshots"] == [
        {
            "id": "abcdef1234567890",
            "short_id": "abcdef12",
            "time": "2026-06-01T12:00:00+00:00",
            "hostname": "host1",
            "paths": ["/data"],
            "tags": [],
            "present": True,
        }
    ]
    service._store.growth_points.assert_called_once_with("repo-id", location_id="loc-id")


def test_get_backup_stats_returns_none_on_cache_miss(tmp_path: Path) -> None:
    service = _make_full_service(tmp_path, cached=None)
    result = service.get_backup_stats("j", "p")
    assert result is None


def test_get_backup_stats_returns_none_when_no_store() -> None:
    service = BackupStatsService()
    assert service.get_backup_stats("j", "p") is None


def test_get_backup_stats_returns_none_when_no_config_service() -> None:
    store = MagicMock()
    service = BackupStatsService(store)
    assert service.get_backup_stats("j", "p") is None
    store.resolve_location.assert_not_called()


def test_get_backup_stats_returns_none_for_unknown_job_or_backup(tmp_path: Path) -> None:
    service = _make_full_service(tmp_path, cached={"job": "j", "backup": "p"})
    assert service.get_backup_stats("missing-job", "p") is None
    assert service.get_backup_stats("j", "missing-backup") is None


def test_get_all_backup_stats_delegates_to_store(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[jobs.j.backup.p]
repository = "/repo-p"
sources = ["/data"]
password = "secret-value"

[jobs.j.backup.q]
repository = "/repo-q"
sources = ["/data"]
password = "secret-value"
""".strip(),
        encoding="utf-8",
    )
    store = MagicMock()

    def _resolve(location_key: str) -> dict[str, object] | None:
        return {
            "local:/repo-p": {
                "repository_id": "repo-p",
                "location_id": "loc-p",
                "display_repository": "/repo-p",
            },
            "local:/repo-q": {
                "repository_id": "repo-q",
                "location_id": "loc-q",
                "display_repository": "/repo-q",
            },
        }.get(location_key)

    store.resolve_location.side_effect = _resolve
    store.list_present_artifacts.return_value = []
    store.growth_points.return_value = []
    service = BackupStatsService(store, config_service=ConfigService(config_path))

    result = service.get_all_backup_stats()

    assert [(view["job"], view["backup"]) for view in result] == [("j", "p"), ("j", "q")]
    assert [(view["repository_id"], view["location_id"]) for view in result] == [
        ("repo-p", "loc-p"),
        ("repo-q", "loc-q"),
    ]
    assert store.list_present_artifacts.call_count == 2
    assert store.growth_points.call_count == 2
    store.growth_points.assert_any_call("repo-p", location_id="loc-p")
    store.growth_points.assert_any_call("repo-q", location_id="loc-q")


def test_get_all_backup_stats_returns_empty_when_no_store() -> None:
    service = BackupStatsService()
    assert service.get_all_backup_stats() == []


def test_get_all_backup_stats_returns_empty_when_no_config_service() -> None:
    store = MagicMock()
    service = BackupStatsService(store)
    assert service.get_all_backup_stats() == []
    store.resolve_location.assert_not_called()


def test_refresh_all_backup_stats_refreshes_each_configured_backup(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[jobs.j.backup.p]
repository = "/repo-p"
sources = ["/data"]
password = "secret-value"

[jobs.j.backup.q]
repository = "/repo-q"
sources = ["/data"]
password = "secret-value"
""".strip(),
        encoding="utf-8",
    )
    service = BackupStatsService(MagicMock(), config_service=ConfigService(config_path))
    service.refresh_backup_stats = AsyncMock()  # type: ignore[method-assign]

    asyncio.run(service.refresh_all_backup_stats())

    service.refresh_backup_stats.assert_has_calls(
        [call("j", "p"), call("j", "q")],
        any_order=True,
    )
    assert service.refresh_backup_stats.call_count == 2


def test_refresh_all_backup_stats_isolates_errors_per_backup(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[jobs.j.backup.p]
repository = "/repo-p"
sources = ["/data"]
password = "secret-value"

[jobs.j.backup.q]
repository = "/repo-q"
sources = ["/data"]
password = "secret-value"
""".strip(),
        encoding="utf-8",
    )
    service = BackupStatsService(MagicMock(), config_service=ConfigService(config_path))

    async def _flaky(job: str, backup: str) -> dict[str, object] | None:
        if backup == "p":
            raise RuntimeError("unreachable repository")
        return {"job": job, "backup": backup}

    service.refresh_backup_stats = AsyncMock(side_effect=_flaky)  # type: ignore[method-assign]

    asyncio.run(service.refresh_all_backup_stats())

    assert service.refresh_backup_stats.call_count == 2


def test_refresh_all_backup_stats_returns_when_no_config_service() -> None:
    service = BackupStatsService(MagicMock())
    asyncio.run(service.refresh_all_backup_stats())


def test_refresh_backup_stats_delegates_to_collector() -> None:
    store = MagicMock()
    collector = MagicMock()
    collected = {"job": "j", "backup": "p", "snapshots_count": 3}
    collector.collect_and_store_async = AsyncMock(return_value=collected)
    service = BackupStatsService(store, collector)

    result = asyncio.run(service.refresh_backup_stats("j", "p"))

    collector.collect_and_store_async.assert_awaited_once_with("j", "p")
    assert result == collected


def test_refresh_backup_stats_returns_none_without_collector() -> None:
    store = MagicMock()
    service = BackupStatsService(store)

    result = asyncio.run(service.refresh_backup_stats("j", "p"))

    assert result is None
