import asyncio
from pathlib import Path
from typing import Any, Callable, cast
from unittest.mock import AsyncMock, patch

import pytest

from src.core.subprocesses import StreamedCommandResult
from src.models.resolved_config import ResolvedBackupConfig
from src.services.config import ConfigService
from src.services.errors import ServiceError
from src.services.repository_artifact_store import RepositoryArtifactStore
from src.services.repository_browser import (
    BROWSE_INDEX_CACHE_MAX_BYTES_TOTAL,
    BROWSE_PAGE_SIZE,
    BROWSE_TIMEOUT_ENV,
    RepositoryBrowserService,
    ResticRecursiveLsResult,
    _run_restic_ls_recursive,
)


def _write_browser_config(path: Path) -> None:
    path.write_text(
        """
[jobs.demo.backup.local]
repository = "/repo/secret-value"
sources = ["/data"]
password = "secret-value"
""".strip(),
        encoding="utf-8",
    )


def _service(tmp_path: Path) -> RepositoryBrowserService:
    config_path = tmp_path / "config.toml"
    _write_browser_config(config_path)
    return RepositoryBrowserService(ConfigService(config_path))


def _stream_result(
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
    calls: list[dict[str, Any]] | None = None,
    chunk_size: int | None = None,
) -> AsyncMock:
    """Build an ``AsyncMock`` that stands in for ``stream_command``.

    Feeds ``on_stdout``/``on_stderr`` callbacks with the configured output
    (optionally split into ``chunk_size``-sized pieces to exercise incremental
    parsing) before returning a :class:`StreamedCommandResult`.
    """

    def _feed(callback: Callable[[bytes], None] | None, text: str) -> None:
        if not text or callback is None:
            return
        data = text.encode()
        if chunk_size is None:
            callback(data)
            return
        for start in range(0, len(data), chunk_size):
            callback(data[start : start + chunk_size])

    async def fake_stream_command(cmd: list[str], **kwargs: Any) -> StreamedCommandResult:
        if calls is not None:
            calls.append({"cmd": cmd, "kwargs": kwargs})
        _feed(kwargs.get("on_stdout"), stdout)
        _feed(kwargs.get("on_stderr"), stderr)
        return StreamedCommandResult(returncode=returncode)

    return AsyncMock(side_effect=fake_stream_command)


def _stream_raises(exc: BaseException) -> AsyncMock:
    async def fake_stream_command(cmd: list[str], **kwargs: Any) -> StreamedCommandResult:
        raise exc

    return AsyncMock(side_effect=fake_stream_command)


def _assert_has_secret(value: object) -> None:
    assert "secret-value" in repr(value)


def _active_location(tmp_path: Path) -> tuple[RepositoryBrowserService, str]:
    service, store = _make_service_with_store(tmp_path)
    store.persist_refresh(
        job="demo",
        backup="local",
        repository="/repo/secret-value",
        backend_repository_id="repo-id",
        artifacts=[{"id": "abcdef1234567890", "paths": ["/data"]}],
        stats={"mode": "raw-data", "total_size_bytes": 1, "snapshots_count": 1},
        observed_at="2026-06-01T12:00:00+00:00",
    )
    location = store.resolve_location("local:/repo/secret-value")
    assert location is not None
    return service, str(location["location_id"])


def _browse_active_location(
    service: RepositoryBrowserService, location_id: str, snapshot_path: str = "/", page: int = 1
) -> dict[str, object]:
    view = asyncio.run(
        service.browse_location_snapshot_view(
            location_id=location_id,
            snapshot_id="abcdef1234567890",
            path=snapshot_path,
            page=page,
        )
    )
    return cast(dict[str, object], view)


def test_repository_browser_overview_returns_empty_without_store(tmp_path: Path) -> None:
    service = _service(tmp_path)

    overview = service.repository_overview_view()

    assert overview == {"repositories": [], "error": None}


def test_repository_browser_location_browse_lists_direct_children(tmp_path: Path) -> None:
    service, location_id = _active_location(tmp_path)
    output = "\n".join(
        [
            '{"message_type": "snapshot", "id": "abcdef1234567890"}',
            ('{"message_type": "node", "type": "dir", "path": "/data", ' '"mode": "drwxr-xr-x"}'),
            (
                '{"message_type": "node", "type": "file", "path": "/data/file.txt", '
                '"size": 12, "mtime": "2026-05-27T00:00:00Z"}'
            ),
            (
                '{"struct_type": "node", "type": "file", '
                '"path": "/data/nested/deep.txt", "size": 44}'
            ),
            '{"struct_type": "node", "type": "file", "path": "/other.txt", "size": 1}',
        ]
    )

    stream_calls: list[dict[str, Any]] = []
    with patch(
        "src.services.repository_browser.stream_command",
        _stream_result(output, calls=stream_calls, chunk_size=37),
    ):
        view = _browse_active_location(service, location_id, "/data")

    assert view["ok"] is True
    assert view["path"] == "/data"
    assert view["parent_path"] == "/"
    assert view["entries"] == [
        {
            "name": "nested",
            "path": "/data/nested",
            "type": "dir",
            "size": None,
            "mode": None,
            "mtime": None,
            "browse": {"path": "/data/nested"},
            "browse_url": (
                f"/repositories/locations/{location_id}/snapshots/abcdef1234567890/browse"
                "?path=%2Fdata%2Fnested"
            ),
        },
        {
            "name": "file.txt",
            "path": "/data/file.txt",
            "type": "file",
            "size": 12,
            "mode": None,
            "mtime": "2026-05-27T00:00:00Z",
            "browse": None,
            "browse_url": None,
        },
    ]
    assert stream_calls[0]["cmd"] == [
        "restic",
        "--repo",
        "/repo/secret-value",
        "ls",
        "--json",
        "abcdef1234567890",
        "/data",
    ]
    assert stream_calls[0]["kwargs"]["env"]["RESTIC_PASSWORD"] == "secret-value"
    assert stream_calls[0]["kwargs"]["timeout"] == 30


def test_repository_browser_browse_urls_are_urlencoded(tmp_path: Path) -> None:
    service, location_id = _active_location(tmp_path)
    output = "\n".join(
        [
            '{"message_type": "snapshot", "id": "abcdef1234567890"}',
            ('{"message_type": "node", "type": "dir", ' '"path": "/data/a & b?#% dir"}'),
        ]
    )

    with patch(
        "src.services.repository_browser.stream_command",
        _stream_result(output),
    ):
        view = _browse_active_location(service, location_id, "/data")

    assert view["breadcrumbs"] == [
        {
            "name": "/",
            "path": "/",
            "browse_url": (
                f"/repositories/locations/{location_id}/snapshots/abcdef1234567890/browse"
                "?path=%2F"
            ),
        },
        {
            "name": "data",
            "path": "/data",
            "browse_url": (
                f"/repositories/locations/{location_id}/snapshots/abcdef1234567890/browse"
                "?path=%2Fdata"
            ),
        },
    ]
    entries = view["entries"]
    assert entries == [
        {
            "name": "a & b?#% dir",
            "path": "/data/a & b?#% dir",
            "type": "dir",
            "size": None,
            "mode": None,
            "mtime": None,
            "browse": {"path": "/data/a & b?#% dir"},
            "browse_url": (
                f"/repositories/locations/{location_id}/snapshots/abcdef1234567890/browse"
                "?path=%2Fdata%2Fa+%26+b%3F%23%25+dir"
            ),
        }
    ]


@pytest.mark.parametrize(
    ("stdout", "code"),
    [
        ("", "empty_response"),
        ('{"unexpected": true}', "unexpected_response"),
        ('{"type": "snapshot", "id": "abcdef1234567890"}', "unexpected_response"),
        ('["bad"]', "unexpected_response"),
        ("{", "invalid_json"),
    ],
)
def test_repository_browser_handles_empty_and_unexpected_json(
    tmp_path: Path, stdout: str, code: str
) -> None:
    service, location_id = _active_location(tmp_path)

    with patch(
        "src.services.repository_browser.stream_command",
        _stream_result(stdout),
    ):
        view = _browse_active_location(service, location_id, "/")

    error = _error(view)
    assert view["ok"] is False
    assert view["entries"] == []
    assert error["code"] == code


def test_repository_browser_returns_raw_restic_errors_timeouts_and_os_errors(
    tmp_path: Path,
) -> None:
    service, location_id = _active_location(tmp_path)

    with patch(
        "src.services.repository_browser.stream_command",
        _stream_result("stdout secret-value", "stderr secret-value", returncode=2),
    ):
        restic_error = _browse_active_location(service, location_id, "/")
    with patch(
        "src.services.repository_browser.stream_command",
        _stream_raises(asyncio.TimeoutError()),
    ):
        timeout_error = _browse_active_location(service, location_id, "/")
    with patch(
        "src.services.repository_browser.stream_command",
        _stream_raises(OSError("missing secret-value")),
    ):
        os_error = _browse_active_location(service, location_id, "/")

    assert _error(restic_error)["code"] == "restic_error"
    assert "secret-value" in str(_error(restic_error)["message"])
    assert _error(timeout_error)["code"] == "timeout"
    assert "30 seconds" in str(_error(timeout_error)["message"])
    assert BROWSE_TIMEOUT_ENV in str(_error(timeout_error)["message"])
    assert _error(os_error)["code"] == "os_error"
    assert "missing secret-value" in str(_error(os_error)["message"])
    _assert_has_secret(restic_error)
    _assert_has_secret(timeout_error)
    _assert_has_secret(os_error)


def test_repository_browser_uses_browse_timeout_env_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, location_id = _active_location(tmp_path)
    monkeypatch.setenv(BROWSE_TIMEOUT_ENV, "75")
    stream_calls: list[dict[str, Any]] = []

    with patch(
        "src.services.repository_browser.stream_command",
        _stream_result(
            '{"message_type": "snapshot", "id": "abcdef1234567890"}',
            calls=stream_calls,
        ),
    ):
        view = _browse_active_location(service, location_id, "/")

    assert view["ok"] is True
    assert stream_calls[0]["kwargs"]["timeout"] == 75


def test_repository_browser_location_browse_cancellation_runs_process_group_cleanup_and_propagates(
    tmp_path: Path,
) -> None:
    """Cancelling an awaited browse must reach ``stream_command`` and its cleanup.

    ``browse_location_snapshot_view``/``_run_restic_ls`` are fully async and await
    ``stream_command`` directly (no thread offload, no exception swallowing);
    this asserts that cancelling the awaiting task propagates
    ``asyncio.CancelledError`` through the service after the central helper's
    process-group cleanup observably ran -- mirroring how
    ``stream_command``/``run_command`` resist and complete cleanup under
    cancellation (see ``test_core/test_subprocesses.py``).
    """
    service, location_id = _active_location(tmp_path)
    cleanup_ran = asyncio.Event()
    started = asyncio.Event()

    async def fake_stream_command(cmd: list[str], **kwargs: Any) -> StreamedCommandResult:
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            # Stand-in for `_terminate_process_group_resisting_cancellation`:
            # cleanup observably completes before cancellation propagates.
            cleanup_ran.set()
            raise
        return StreamedCommandResult(returncode=0)

    async def scenario() -> None:
        with patch(
            "src.services.repository_browser.stream_command",
            AsyncMock(side_effect=fake_stream_command),
        ):
            task = asyncio.create_task(
                service.browse_location_snapshot_view(
                    location_id=location_id, snapshot_id="abcdef1234567890"
                )
            )
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert cleanup_ran.is_set()

    asyncio.run(scenario())


def test_repository_browser_location_browse_cancellation_propagates(tmp_path: Path) -> None:
    """The HTMX-facing location browse view must not swallow cancellation."""
    service, location_id = _active_location(tmp_path)
    started = asyncio.Event()

    async def fake_stream_command(cmd: list[str], **kwargs: Any) -> StreamedCommandResult:
        started.set()
        await asyncio.sleep(10)
        return StreamedCommandResult(returncode=0)

    async def scenario() -> None:
        with patch(
            "src.services.repository_browser.stream_command",
            AsyncMock(side_effect=fake_stream_command),
        ):
            task = asyncio.create_task(
                service.browse_location_snapshot_view(
                    location_id=location_id, snapshot_id="abcdef1234567890"
                )
            )
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("../bad", "/"), "Invalid snapshot id"),
        (("abcdef1234567890", "relative"), "Snapshot path must be absolute"),
        (("abcdef1234567890", "/data/../secret"), "Invalid snapshot path"),
        (("abcdef1234567890", "/data//secret"), "Invalid snapshot path"),
        (("abcdef1234567890", "/data/\nsecret"), "Invalid snapshot path"),
        (("abcdef1234567890", "/data/\rsecret"), "Invalid snapshot path"),
    ],
)
def test_repository_browser_rejects_invalid_parameters(
    tmp_path: Path, args: tuple[str, str], message: str
) -> None:
    service, location_id = _active_location(tmp_path)
    snapshot_id, snapshot_path = args

    with pytest.raises(ServiceError, match=message):
        asyncio.run(
            service.browse_location_snapshot_view(
                location_id=location_id, snapshot_id=snapshot_id, path=snapshot_path
            )
        )


def test_repository_browser_returns_raw_secret_paths_in_location_browse_view(
    tmp_path: Path,
) -> None:
    service, location_id = _active_location(tmp_path)
    output = "\n".join(
        [
            '{"struct_type": "snapshot", "id": "abcdef1234567890"}',
            '{"struct_type": "node", "type": "dir", "path": "/data/secret-value"}',
            (
                '{"message_type": "node", "type": "file", '
                '"path": "/data/secret-value/file.txt", "size": 12}'
            ),
        ]
    )

    with patch(
        "src.services.repository_browser.stream_command",
        _stream_result(output),
    ):
        view = _browse_active_location(service, location_id, "/data")

    assert view["entries"] == [
        {
            "name": "secret-value",
            "path": "/data/secret-value",
            "type": "dir",
            "size": None,
            "mode": None,
            "mtime": None,
            "browse": {"path": "/data/secret-value"},
            "browse_url": (
                f"/repositories/locations/{location_id}/snapshots/abcdef1234567890/browse"
                "?path=%2Fdata%2Fsecret-value"
            ),
        }
    ]
    _assert_has_secret(view)


def test_repository_browser_pages_large_directories(tmp_path: Path) -> None:
    service, location_id = _active_location(tmp_path)
    output = "\n".join(
        ['{"message_type": "snapshot", "id": "abcdef1234567890"}']
        + [
            (
                '{"message_type": "node", "type": "file", '
                f'"path": "/data/file-{index:03}.txt", "size": {index}}}'
            )
            for index in range(BROWSE_PAGE_SIZE + 1)
        ]
    )
    stream_calls: list[dict[str, Any]] = []

    with patch(
        "src.services.repository_browser.stream_command",
        _stream_result(output, calls=stream_calls),
    ):
        first_page = _browse_active_location(service, location_id, "/data")
        second_page = _browse_active_location(service, location_id, "/data", 2)

    first_page_entries = cast(list[dict[str, object]], first_page["entries"])
    assert len(first_page_entries) == BROWSE_PAGE_SIZE
    assert first_page["page"] == 1
    assert first_page["total_entries"] == BROWSE_PAGE_SIZE + 1
    assert first_page["total_pages"] == 2
    assert first_page["previous_url"] is None
    assert first_page["next_url"] == (
        f"/repositories/locations/{location_id}/snapshots/abcdef1234567890/browse"
        "?path=%2Fdata&page=2"
    )
    assert second_page["entries"] == [
        {
            "name": f"file-{BROWSE_PAGE_SIZE:03}.txt",
            "path": f"/data/file-{BROWSE_PAGE_SIZE:03}.txt",
            "type": "file",
            "size": BROWSE_PAGE_SIZE,
            "mode": None,
            "mtime": None,
            "browse": None,
            "browse_url": None,
        }
    ]
    assert second_page["previous_url"] == (
        f"/repositories/locations/{location_id}/snapshots/abcdef1234567890/browse" "?path=%2Fdata"
    )
    assert second_page["next_url"] is None
    assert len(stream_calls) == 1


def test_repository_browser_recursive_prefetch_caches_subtree_directories(
    tmp_path: Path,
) -> None:
    service, location_id = _active_location(tmp_path)
    output = "\n".join(
        [
            '{"message_type": "snapshot", "id": "abcdef1234567890"}',
            '{"message_type": "node", "type": "dir", "path": "/data"}',
            '{"message_type": "node", "type": "dir", "path": "/data/photos"}',
            (
                '{"message_type": "node", "type": "file", '
                '"path": "/data/photos/image.jpg", "size": 42}'
            ),
            '{"message_type": "node", "type": "file", "path": "/data/readme.txt", "size": 5}',
        ]
    )
    stream_calls: list[dict[str, Any]] = []

    with patch(
        "src.services.repository_browser.stream_command",
        _stream_result(output, calls=stream_calls, chunk_size=31),
    ):
        root_view = asyncio.run(
            service.prefetch_location_snapshot_view(
                location_id=location_id,
                snapshot_id="abcdef1234567890",
                path="/",
            )
        )
        data_view = _browse_active_location(service, location_id, "/data")
        photos_view = _browse_active_location(service, location_id, "/data/photos")

    assert root_view["ok"] is True
    assert root_view["indexed_paths"] == 3
    assert root_view["indexed_bytes"] > 0
    assert root_view["indexed_bytes_limit"] == BROWSE_INDEX_CACHE_MAX_BYTES_TOTAL
    assert [entry["name"] for entry in cast(list[dict[str, object]], root_view["entries"])] == [
        "data"
    ]
    assert [entry["name"] for entry in cast(list[dict[str, object]], data_view["entries"])] == [
        "photos",
        "readme.txt",
    ]
    assert [entry["name"] for entry in cast(list[dict[str, object]], photos_view["entries"])] == [
        "image.jpg"
    ]
    assert len(stream_calls) == 1
    assert stream_calls[0]["cmd"] == [
        "restic",
        "--repo",
        "/repo/secret-value",
        "ls",
        "--json",
        "--recursive",
        "abcdef1234567890",
        "/",
    ]


def test_repository_browser_recursive_ls_failure_discards_partial_index(
    tmp_path: Path,
) -> None:
    backup = ResolvedBackupConfig(repository="/repo/secret-value")
    partial_output = "\n".join(
        [
            '{"message_type": "snapshot", "id": "abcdef1234567890"}',
            '{"message_type": "node", "type": "dir", "path": "/data"}',
            '{"message_type": "node", "type": "file", "path": "/data/file.txt", "size": 5}',
        ]
    )

    with patch(
        "src.services.repository_browser.stream_command",
        _stream_result(partial_output, stderr="restic failed", returncode=2),
    ):
        result = asyncio.run(
            _run_restic_ls_recursive(
                backup,
                "abcdef1234567890",
                "/",
                "demo",
                "local",
            )
        )

    assert result.ok is False
    assert result.directories == {}
    assert result.error is not None
    assert result.error["code"] == "restic_error"


def test_repository_browser_index_cache_evicts_whole_old_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.services.repository_browser.BROWSE_INDEX_CACHE_MAX_BYTES_TOTAL", 1)
    service = _service(tmp_path)
    backup = ResolvedBackupConfig(repository="/repo/secret-value")
    first = ResticRecursiveLsResult(
        True,
        {
            "/first": [
                {
                    "name": "a.txt",
                    "path": "/first/a.txt",
                    "type": "file",
                    "size": 1,
                    "mode": None,
                    "mtime": None,
                    "browse": None,
                    "browse_url": None,
                }
            ],
            "/first/nested": [],
        },
    )
    second = ResticRecursiveLsResult(
        True,
        {
            "/second": [
                {
                    "name": "b.txt",
                    "path": "/second/b.txt",
                    "type": "file",
                    "size": 1,
                    "mode": None,
                    "mtime": None,
                    "browse": None,
                    "browse_url": None,
                }
            ]
        },
    )

    service._remember_recursive_browse_result(backup, "abcdef1234567890", "/first", first)
    service._remember_recursive_browse_result(backup, "fedcba9876543210", "/second", second)

    assert service._browse_index_cache_path_count() == 1
    assert ("/repo/secret-value", "abcdef1234567890", "/first") not in service._browse_cache
    assert ("/repo/secret-value", "abcdef1234567890", "/first/nested") not in service._browse_cache
    assert ("/repo/secret-value", "fedcba9876543210", "/second") in service._browse_cache


def test_repository_browser_reindex_same_snapshot_replaces_previous_group(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    backup = ResolvedBackupConfig(repository="/repo/secret-value")
    root_index = ResticRecursiveLsResult(
        True,
        {
            "/": [
                {
                    "name": "data",
                    "path": "/data",
                    "type": "dir",
                    "size": None,
                    "mode": None,
                    "mtime": None,
                    "browse": None,
                    "browse_url": None,
                }
            ],
            "/data": [],
        },
    )
    subtree_index = ResticRecursiveLsResult(
        True,
        {
            "/data/photos": [
                {
                    "name": "a.jpg",
                    "path": "/data/photos/a.jpg",
                    "type": "file",
                    "size": 1,
                    "mode": None,
                    "mtime": None,
                    "browse": None,
                    "browse_url": None,
                }
            ]
        },
    )

    service._remember_recursive_browse_result(backup, "abcdef1234567890", "/", root_index)
    assert service._browse_index_cache_path_count() == 2
    assert len(service._browse_index_groups) == 1

    service._remember_recursive_browse_result(
        backup, "abcdef1234567890", "/data/photos", subtree_index
    )

    assert len(service._browse_index_groups) == 1
    assert service._browse_index_cache_path_count() == 1
    assert ("/repo/secret-value", "abcdef1234567890", "/") not in service._browse_cache
    assert ("/repo/secret-value", "abcdef1234567890", "/data") not in service._browse_cache
    assert (
        "/repo/secret-value",
        "abcdef1234567890",
        "/data/photos",
    ) in service._browse_cache


@pytest.mark.parametrize("page", [0, -1, 2])
def test_repository_browser_rejects_invalid_pages(tmp_path: Path, page: int) -> None:
    service, location_id = _active_location(tmp_path)
    output = '{"message_type": "snapshot", "id": "abcdef1234567890"}'

    with patch(
        "src.services.repository_browser.stream_command",
        _stream_result(output),
    ):
        with pytest.raises(ServiceError, match="Invalid browse page"):
            _browse_active_location(service, location_id, "/", page)


def _make_service_with_store(
    tmp_path: Path,
) -> tuple[RepositoryBrowserService, RepositoryArtifactStore]:
    config_path = tmp_path / "config.toml"
    _write_browser_config(config_path)
    store = RepositoryArtifactStore(db_path=tmp_path / "appdata.db")
    service = RepositoryBrowserService(ConfigService(config_path), store)
    return service, store


class _FakeBackupStatsService:
    def get_backup_growth_data(self, job: str, backup: str) -> dict[str, object]:
        return {"has_data": True, "labels": [f"{job}.{backup}"], "data": [1], "formatted": ["1 B"]}


def _seed_snapshots(
    store: RepositoryArtifactStore,
    job: str,
    backup: str,
    snapshots: list[dict[str, object]],
    *,
    updated_at: str,
) -> None:
    """Seed repository artifacts through the production refresh API."""
    store.persist_refresh(
        job=job,
        backup=backup,
        repository="/repo/secret-value",
        backend_repository_id="repo-id",
        artifacts=snapshots,
        stats={"mode": "raw-data", "total_size_bytes": 1, "snapshots_count": len(snapshots)},
        observed_at=updated_at,
    )


def test_get_repositories_view_flattens_locations_for_one_physical_repository(
    tmp_path: Path,
) -> None:
    service, store = _make_service_with_store(tmp_path)
    assert store.persist_refresh(
        job="demo",
        backup="local",
        repository="/repo/secret-value",
        backend_repository_id="repo-id",
        artifacts=[{"id": "abcdef1234567890", "paths": ["/data"]}],
        stats={"mode": "raw-data", "total_size_bytes": 1, "snapshots_count": 1},
        observed_at="2026-06-01T12:00:00+00:00",
    )
    assert store.persist_refresh(
        job="demo",
        backup="local",
        repository="/repo/other-location",
        backend_repository_id="repo-id",
        artifacts=[{"id": "bbcdef1234567890", "paths": ["/data"]}],
        stats={"mode": "raw-data", "total_size_bytes": 2, "snapshots_count": 2},
        observed_at="2026-06-01T12:02:00+00:00",
    )

    view = asyncio.run(service.get_repositories_view())
    repositories = cast(list[dict[str, Any]], view["repositories"])

    assert len(repositories) == 1
    repository = repositories[0]
    locations = cast(list[dict[str, object]], repository["locations"])
    assert len(locations) == 2

    active = next(loc for loc in locations if loc["display_repository"] == "/repo/secret-value")
    assert active["is_active"] is True
    assert active["label"] == "demo.local"
    assert active["config_refs"] == [{"job": "demo", "backup": "local"}]
    assert active["delete_url"] == f"/repositories/locations/{active['location_id']}/delete"
    assert active["merge_url"] == f"/repositories/locations/{active['location_id']}/merge"
    assert active["view_url"] == f"/repositories/locations/{active['location_id']}/snapshots"

    inactive = next(loc for loc in locations if loc["display_repository"] == "/repo/other-location")
    assert inactive["is_active"] is False
    assert inactive["label"] == "Inactive"
    assert inactive["config_refs"] == []
    assert len(cast(list[dict[str, object]], active["merge_targets"])) == 1
    assert "last_seen_at" not in active
    assert "last_seen_at" not in inactive


def test_get_repositories_view_keeps_different_backend_repository_ids_separate(
    tmp_path: Path,
) -> None:
    service, store = _make_service_with_store(tmp_path)
    assert store.persist_refresh(
        job="demo",
        backup="local",
        repository="/repo/secret-value",
        backend_repository_id="repo-id-1",
        artifacts=[{"id": "abcdef1234567890", "paths": ["/data"]}],
        stats={"mode": "raw-data", "total_size_bytes": 1, "snapshots_count": 1},
        observed_at="2026-06-01T12:00:00+00:00",
    )
    assert store.persist_refresh(
        job="demo",
        backup="local2",
        repository="/repo/another-repo",
        backend_repository_id="repo-id-2",
        artifacts=[{"id": "bbcdef1234567890", "paths": ["/data"]}],
        stats={"mode": "raw-data", "total_size_bytes": 2, "snapshots_count": 2},
        observed_at="2026-06-01T12:02:00+00:00",
    )

    view = asyncio.run(service.get_repositories_view())
    repositories = cast(list[dict[str, Any]], view["repositories"])

    assert len(repositories) == 2
    titles = {repository["title"] for repository in repositories}
    assert titles == {"secret-value", "another-repo"}


def test_get_repositories_view_titles_dedup_and_join_distinct_basenames(
    tmp_path: Path,
) -> None:
    service, store = _make_service_with_store(tmp_path)
    assert store.persist_refresh(
        job="demo",
        backup="local",
        repository="/repo/secret-value",
        backend_repository_id="repo-id",
        artifacts=[{"id": "abcdef1234567890", "paths": ["/data"]}],
        stats={"mode": "raw-data", "total_size_bytes": 1, "snapshots_count": 1},
        observed_at="2026-06-01T12:00:00+00:00",
    )
    assert store.persist_refresh(
        job="demo",
        backup="local",
        repository="/repo/NeueNameRepo",
        backend_repository_id="repo-id",
        artifacts=[{"id": "bbcdef1234567890", "paths": ["/data"]}],
        stats={"mode": "raw-data", "total_size_bytes": 2, "snapshots_count": 2},
        observed_at="2026-06-01T12:02:00+00:00",
    )

    view = asyncio.run(service.get_repositories_view())
    repositories = cast(list[dict[str, Any]], view["repositories"])

    assert len(repositories) == 1
    assert repositories[0]["title"] == "NeueNameRepo / secret-value"


def _error(view: dict[str, object]) -> dict[str, Any]:
    error = view["error"]
    assert isinstance(error, dict)
    return error


def _write_multi_job_config(path: Path) -> None:
    path.write_text(
        """
[jobs.demo.backup.local]
repository = "/repo/secret-value"
sources = ["/data"]
password = "secret-value"

[jobs.demo.backup.local2]
repository = "/repo/other-location"
sources = ["/data"]
password = "secret-value"

[jobs.other.backup.main]
repository = "/repo/another-repo"
sources = ["/data"]
password = "other-secret"
""".strip(),
        encoding="utf-8",
    )


def _make_service_with_multi_job_config(
    tmp_path: Path,
) -> tuple[RepositoryBrowserService, RepositoryArtifactStore]:
    config_path = tmp_path / "config.toml"
    _write_multi_job_config(config_path)
    store = RepositoryArtifactStore(db_path=tmp_path / "appdata.db")
    service = RepositoryBrowserService(ConfigService(config_path), store)
    return service, store


def test_resolve_backup_config_for_location_returns_none_for_unknown_location(
    tmp_path: Path,
) -> None:
    service, _store = _make_service_with_multi_job_config(tmp_path)

    assert service.resolve_backup_config_for_location("does-not-exist") is None


def test_resolve_backup_config_for_location_uses_own_active_config(tmp_path: Path) -> None:
    service, store = _make_service_with_multi_job_config(tmp_path)
    store.persist_refresh(
        job="demo",
        backup="local",
        repository="/repo/secret-value",
        backend_repository_id="repo-id",
        artifacts=[{"id": "abcdef1234567890", "paths": ["/data"]}],
        stats={"mode": "raw-data", "total_size_bytes": 1, "snapshots_count": 1},
        observed_at="2026-06-01T12:00:00+00:00",
    )
    location = store.resolve_location("local:/repo/secret-value")
    assert location is not None

    resolved = service.resolve_backup_config_for_location(str(location["location_id"]))

    assert resolved is not None
    job_name, backup_name, config = resolved
    assert (job_name, backup_name) == ("demo", "local")
    assert config.repository == "/repo/secret-value"


def test_resolve_backup_config_for_location_picks_deterministically_among_active_configs(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[jobs.demo.backup.local]
repository = "/repo/secret-value"
sources = ["/data"]
password = "secret-value"

[jobs.zzz.backup.local]
repository = "/repo/secret-value"
sources = ["/data"]
password = "secret-value"
""".strip(),
        encoding="utf-8",
    )
    store = RepositoryArtifactStore(db_path=tmp_path / "appdata.db")
    service = RepositoryBrowserService(ConfigService(config_path), store)
    store.persist_refresh(
        job="demo",
        backup="local",
        repository="/repo/secret-value",
        backend_repository_id="repo-id",
        artifacts=[{"id": "abcdef1234567890", "paths": ["/data"]}],
        stats={"mode": "raw-data", "total_size_bytes": 1, "snapshots_count": 1},
        observed_at="2026-06-01T12:00:00+00:00",
    )
    location = store.resolve_location("local:/repo/secret-value")
    assert location is not None

    resolved = service.resolve_backup_config_for_location(str(location["location_id"]))

    assert resolved is not None
    job_name, backup_name, _config = resolved
    assert (job_name, backup_name) == ("demo", "local")


def test_resolve_backup_config_for_location_borrows_donor_for_inactive_location(
    tmp_path: Path,
) -> None:
    service, store = _make_service_with_multi_job_config(tmp_path)
    # The DB still knows an inactive location sharing the same repo-id.
    store.persist_refresh(
        job="demo",
        backup="local2",
        repository="/repo/other-location",
        backend_repository_id="repo-id",
        artifacts=[{"id": "abcdef1234567890", "paths": ["/data"]}],
        stats={"mode": "raw-data", "total_size_bytes": 1, "snapshots_count": 1},
        observed_at="2026-06-01T12:00:00+00:00",
    )
    store.persist_refresh(
        job="demo",
        backup="local2",
        repository="/repo/orphaned",
        backend_repository_id="repo-id",
        artifacts=[{"id": "bbcdef1234567890", "paths": ["/data"]}],
        stats={"mode": "raw-data", "total_size_bytes": 1, "snapshots_count": 1},
        observed_at="2026-06-01T12:01:00+00:00",
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[jobs.demo.backup.local]
repository = "/repo/secret-value"
sources = ["/data"]
password = "secret-value"

[jobs.demo.backup.local2]
repository = "/repo/other-location"
sources = ["/data"]
password = "secret-value"
""".strip(),
        encoding="utf-8",
    )
    orphaned_location = store.resolve_location("local:/repo/orphaned")
    assert orphaned_location is not None

    resolved = service.resolve_backup_config_for_location(str(orphaned_location["location_id"]))

    assert resolved is not None
    job_name, backup_name, config = resolved
    # demo.local2 is the only config currently pointing anywhere within the
    # shared repo-id (at /repo/other-location), so it is the donor. The
    # resolved config's path is the *location's* own path, not the donor's.
    assert (job_name, backup_name) == ("demo", "local2")
    assert config.repository == "/repo/orphaned"


def test_resolve_backup_config_for_location_picks_deterministic_donor_among_candidates(
    tmp_path: Path,
) -> None:
    service, store = _make_service_with_multi_job_config(tmp_path)
    store.persist_refresh(
        job="demo",
        backup="local",
        repository="/repo/secret-value",
        backend_repository_id="repo-id",
        artifacts=[{"id": "abcdef1234567890", "paths": ["/data"]}],
        stats={"mode": "raw-data", "total_size_bytes": 1, "snapshots_count": 1},
        observed_at="2026-06-01T12:00:00+00:00",
    )
    store.persist_refresh(
        job="demo",
        backup="local2",
        repository="/repo/secret-value",
        backend_repository_id="repo-id",
        artifacts=[{"id": "abcdef1234567890", "paths": ["/data"]}],
        stats={"mode": "raw-data", "total_size_bytes": 1, "snapshots_count": 1},
        observed_at="2026-06-01T12:00:00+00:00",
    )
    store.persist_refresh(
        job="demo",
        backup="local",
        repository="/repo/orphaned",
        backend_repository_id="repo-id",
        artifacts=[{"id": "bbcdef1234567890", "paths": ["/data"]}],
        stats={"mode": "raw-data", "total_size_bytes": 1, "snapshots_count": 1},
        observed_at="2026-06-01T12:01:00+00:00",
    )
    orphaned_location = store.resolve_location("local:/repo/orphaned")
    assert orphaned_location is not None

    resolved = service.resolve_backup_config_for_location(str(orphaned_location["location_id"]))

    assert resolved is not None
    job_name, backup_name, config = resolved
    # Both demo.local and demo.local2 currently point at /repo/secret-value
    # (same location_id) -- sorted (job, backup) tuples put "local" first.
    assert (job_name, backup_name) == ("demo", "local")
    assert config.repository == "/repo/orphaned"


def test_resolve_backup_config_for_location_returns_none_without_any_donor(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")
    store = RepositoryArtifactStore(db_path=tmp_path / "appdata.db")
    service = RepositoryBrowserService(ConfigService(config_path), store)
    store.persist_refresh(
        job="ghost",
        backup="local",
        repository="/repo/orphaned",
        backend_repository_id="repo-id",
        artifacts=[{"id": "abcdef1234567890", "paths": ["/data"]}],
        stats={"mode": "raw-data", "total_size_bytes": 1, "snapshots_count": 1},
        observed_at="2026-06-01T12:00:00+00:00",
    )
    orphaned_location = store.resolve_location("local:/repo/orphaned")
    assert orphaned_location is not None

    resolved = service.resolve_backup_config_for_location(str(orphaned_location["location_id"]))

    assert resolved is None


def _orphaned_location_without_donor(tmp_path: Path) -> tuple[RepositoryBrowserService, str]:
    """Build a service/location pair with no active config and no donor."""
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")
    store = RepositoryArtifactStore(db_path=tmp_path / "appdata.db")
    service = RepositoryBrowserService(ConfigService(config_path), store)
    store.persist_refresh(
        job="ghost",
        backup="local",
        repository="/repo/orphaned",
        backend_repository_id="repo-id",
        artifacts=[{"id": "abcdef1234567890", "paths": ["/data"]}],
        stats={"mode": "raw-data", "total_size_bytes": 1, "snapshots_count": 1},
        observed_at="2026-06-01T12:00:00+00:00",
    )
    location = store.resolve_location("local:/repo/orphaned")
    assert location is not None
    return service, str(location["location_id"])


def test_build_adhoc_backup_config_with_password(tmp_path: Path) -> None:
    service, location_id = _orphaned_location_without_donor(tmp_path)

    config = service.build_adhoc_backup_config(
        location_id, password="hunter2", password_env=None, password_file=None
    )

    assert config.repository == "/repo/orphaned"
    assert config.credentials.password == "hunter2"
    assert config.credentials.password_env is None
    assert config.credentials.password_file is None


def test_build_adhoc_backup_config_with_password_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, location_id = _orphaned_location_without_donor(tmp_path)
    monkeypatch.setenv("DK_TEST_ADHOC_PASSWORD", "from-env")

    config = service.build_adhoc_backup_config(
        location_id, password=None, password_env="DK_TEST_ADHOC_PASSWORD", password_file=None
    )

    assert config.repository == "/repo/orphaned"
    assert config.credentials.password == "from-env"
    assert config.credentials.password_env == "DK_TEST_ADHOC_PASSWORD"
    assert config.credentials.password_file is None


def test_build_adhoc_backup_config_with_password_file(tmp_path: Path) -> None:
    service, location_id = _orphaned_location_without_donor(tmp_path)
    password_file = tmp_path / "restic.pw"
    password_file.write_text("file-secret", encoding="utf-8")

    config = service.build_adhoc_backup_config(
        location_id, password=None, password_env=None, password_file=str(password_file)
    )

    assert config.repository == "/repo/orphaned"
    assert config.credentials.password is None
    assert config.credentials.password_env is None
    assert config.credentials.password_file == str(password_file)


def test_build_adhoc_backup_config_rejects_unknown_location(tmp_path: Path) -> None:
    service, _store = _make_service_with_multi_job_config(tmp_path)

    with pytest.raises(ServiceError, match="Location not found"):
        service.build_adhoc_backup_config(
            "does-not-exist", password="x", password_env=None, password_file=None
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"password": None, "password_env": None, "password_file": None},
        {"password": "a", "password_env": "B", "password_file": None},
        {"password": "a", "password_env": None, "password_file": "/x"},
        {"password": None, "password_env": "B", "password_file": "/x"},
        {"password": "a", "password_env": "B", "password_file": "/x"},
    ],
)
def test_build_adhoc_backup_config_rejects_non_exclusive_credentials(
    tmp_path: Path, kwargs: dict[str, str | None]
) -> None:
    service, location_id = _orphaned_location_without_donor(tmp_path)

    with pytest.raises(ServiceError, match="mutually exclusive|is required"):
        service.build_adhoc_backup_config(location_id, **kwargs)


def test_build_adhoc_backup_config_rejects_unset_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, location_id = _orphaned_location_without_donor(tmp_path)
    monkeypatch.delenv("DK_TEST_UNSET_ADHOC_VAR", raising=False)

    with pytest.raises(ServiceError, match="DK_TEST_UNSET_ADHOC_VAR.*not set"):
        service.build_adhoc_backup_config(
            location_id, password=None, password_env="DK_TEST_UNSET_ADHOC_VAR", password_file=None
        )


def test_build_adhoc_backup_config_rejects_empty_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, location_id = _orphaned_location_without_donor(tmp_path)
    monkeypatch.setenv("DK_TEST_EMPTY_ADHOC_VAR", "")

    with pytest.raises(ServiceError, match="DK_TEST_EMPTY_ADHOC_VAR.*empty"):
        service.build_adhoc_backup_config(
            location_id, password=None, password_env="DK_TEST_EMPTY_ADHOC_VAR", password_file=None
        )


def test_build_adhoc_backup_config_rejects_relative_password_file(tmp_path: Path) -> None:
    service, location_id = _orphaned_location_without_donor(tmp_path)

    with pytest.raises(ServiceError, match="absolute"):
        service.build_adhoc_backup_config(
            location_id, password=None, password_env=None, password_file="relative/restic.pw"
        )


def test_build_adhoc_backup_config_rejects_missing_password_file(tmp_path: Path) -> None:
    service, location_id = _orphaned_location_without_donor(tmp_path)
    missing_path = tmp_path / "does-not-exist.pw"

    with pytest.raises(ServiceError, match="does not exist"):
        service.build_adhoc_backup_config(
            location_id, password=None, password_env=None, password_file=str(missing_path)
        )


def test_get_location_snapshots_view_reads_snapshots_for_location(tmp_path: Path) -> None:
    service, store = _make_service_with_multi_job_config(tmp_path)
    store.persist_refresh(
        job="demo",
        backup="local",
        repository="/repo/secret-value",
        backend_repository_id="repo-id",
        artifacts=[{"id": "abcdef1234567890", "paths": ["/data"]}],
        stats={"mode": "raw-data", "total_size_bytes": 1, "snapshots_count": 1},
        observed_at="2026-06-01T12:00:00+00:00",
    )
    location = store.resolve_location("local:/repo/secret-value")
    assert location is not None
    location_id = str(location["location_id"])

    view = asyncio.run(service.get_location_snapshots_view(location_id))

    assert view["repository"] == "/repo/secret-value"
    assert view["is_active"] is True
    assert view["label"] == "demo.local"
    assert view["config_refs"] == [{"job": "demo", "backup": "local"}]
    assert view["refresh_url"] == f"/repositories/locations/{location_id}/refresh"
    assert len(view["snapshots"]) == 1
    snapshot = view["snapshots"][0]
    assert snapshot["short_id"] == "abcdef12"
    assert snapshot["paths"] == ["/data"]
    assert snapshot["detail_url"] == f"/repositories/locations/{location_id}/snapshots/abcdef12"
    assert (
        snapshot["restore_url"]
        == f"/repositories/locations/{location_id}/snapshots/abcdef12/restore"
    )


def test_get_location_snapshots_view_includes_active_config_growth_data(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_multi_job_config(config_path)
    store = RepositoryArtifactStore(db_path=tmp_path / "appdata.db")
    service = RepositoryBrowserService(
        ConfigService(config_path),
        store,
        backup_stats_service=cast(Any, _FakeBackupStatsService()),
    )
    store.persist_refresh(
        job="demo",
        backup="local",
        repository="/repo/secret-value",
        backend_repository_id="repo-id",
        artifacts=[{"id": "abcdef1234567890", "paths": ["/data"]}],
        stats={"mode": "raw-data", "total_size_bytes": 1, "snapshots_count": 1},
        observed_at="2026-06-01T12:00:00+00:00",
    )
    location = store.resolve_location("local:/repo/secret-value")
    assert location is not None

    view = asyncio.run(service.get_location_snapshots_view(str(location["location_id"])))

    assert view["growth_data"] == {
        "has_data": True,
        "labels": ["demo.local"],
        "data": [1],
        "formatted": ["1 B"],
    }


def test_get_location_snapshots_view_filters_invalid_snapshot_ids_from_store(
    tmp_path: Path,
) -> None:
    service, store = _make_service_with_multi_job_config(tmp_path)
    store.persist_refresh(
        job="demo",
        backup="local",
        repository="/repo/secret-value",
        backend_repository_id="repo-id",
        artifacts=[
            {"id": "../bad", "paths": ["/ignored"]},
            {"id": "abcdef1234567890", "paths": ["/data"]},
        ],
        stats={"mode": "raw-data", "total_size_bytes": 1, "snapshots_count": 2},
        observed_at="2026-06-01T12:00:00+00:00",
    )
    location = store.resolve_location("local:/repo/secret-value")
    assert location is not None

    view = asyncio.run(service.get_location_snapshots_view(str(location["location_id"])))

    assert len(view["snapshots"]) == 1
    assert view["snapshots"][0]["short_id"] == "abcdef12"


def test_get_location_snapshots_view_marks_inactive_location(tmp_path: Path) -> None:
    service, store = _make_service_with_multi_job_config(tmp_path)
    store.persist_refresh(
        job="demo",
        backup="local",
        repository="/repo/orphaned",
        backend_repository_id="repo-id",
        artifacts=[{"id": "abcdef1234567890", "paths": ["/data"]}],
        stats={"mode": "raw-data", "total_size_bytes": 1, "snapshots_count": 1},
        observed_at="2026-06-01T12:00:00+00:00",
    )
    location = store.resolve_location("local:/repo/orphaned")
    assert location is not None

    view = asyncio.run(service.get_location_snapshots_view(str(location["location_id"])))

    assert view["is_active"] is False
    assert view["label"] == "Inactive"
    assert view["config_refs"] == []
    assert view["refresh_url"] is None


def test_get_location_snapshots_view_raises_not_found_for_unknown_location(
    tmp_path: Path,
) -> None:
    service, _store = _make_service_with_multi_job_config(tmp_path)

    with pytest.raises(ServiceError, match="Location not found"):
        asyncio.run(service.get_location_snapshots_view("does-not-exist"))


def test_get_location_snapshot_view_reads_metadata_without_restore_target(
    tmp_path: Path,
) -> None:
    service, store = _make_service_with_multi_job_config(tmp_path)
    store.persist_refresh(
        job="demo",
        backup="local",
        repository="/repo/secret-value",
        backend_repository_id="repo-id",
        artifacts=[
            {
                "id": "abcdef1234567890",
                "time": "2024-01-01T12:00:00",
                "hostname": "host1",
            }
        ],
        stats={"mode": "raw-data", "total_size_bytes": 1, "snapshots_count": 1},
        observed_at="2026-06-01T12:00:00+00:00",
    )
    location = store.resolve_location("local:/repo/secret-value")
    assert location is not None
    location_id = str(location["location_id"])

    view = asyncio.run(service.get_location_snapshot_view(location_id, "abcdef12"))

    assert "restore_url" not in view
    assert (
        view["browse_url"]
        == f"/repositories/locations/{location_id}/snapshots/abcdef12/browse?path=%2F"
    )
    snapshot = cast(dict[str, Any], view["snapshot"])
    assert snapshot["id"] == "abcdef1234567890"
    assert snapshot["short_id"] == "abcdef12"
    assert view["needs_adhoc_credentials"] is False


def test_get_location_snapshot_view_needs_adhoc_credentials_when_truly_orphaned(
    tmp_path: Path,
) -> None:
    service, location_id = _orphaned_location_without_donor(tmp_path)

    view = asyncio.run(service.get_location_snapshot_view(location_id, "abcdef12"))

    assert view["needs_adhoc_credentials"] is True


def test_get_location_snapshot_view_raises_not_found_for_unknown_location(
    tmp_path: Path,
) -> None:
    service, _store = _make_service_with_multi_job_config(tmp_path)

    with pytest.raises(ServiceError, match="Location not found"):
        asyncio.run(service.get_location_snapshot_view("does-not-exist", "abcdef12"))


def _browse_location(
    service: RepositoryBrowserService, *args: Any, **kwargs: Any
) -> dict[str, object]:
    view = asyncio.run(service.browse_location_snapshot_view(*args, **kwargs))
    return cast(dict[str, object], view)


def test_browse_location_snapshot_view_active_location_uses_active_credentials(
    tmp_path: Path,
) -> None:
    service, store = _make_service_with_multi_job_config(tmp_path)
    store.persist_refresh(
        job="demo",
        backup="local",
        repository="/repo/secret-value",
        backend_repository_id="repo-id",
        artifacts=[{"id": "abcdef1234567890", "paths": ["/data"]}],
        stats={"mode": "raw-data", "total_size_bytes": 1, "snapshots_count": 1},
        observed_at="2026-06-01T12:00:00+00:00",
    )
    location = store.resolve_location("local:/repo/secret-value")
    assert location is not None
    location_id = str(location["location_id"])

    output = "\n".join(
        [
            '{"message_type": "snapshot", "id": "abcdef1234567890"}',
            ('{"message_type": "node", "type": "dir", "path": "/data", ' '"mode": "drwxr-xr-x"}'),
            (
                '{"message_type": "node", "type": "file", "path": "/data/file.txt", '
                '"size": 12, "mtime": "2026-05-27T00:00:00Z"}'
            ),
        ]
    )
    stream_calls: list[dict[str, Any]] = []
    with patch(
        "src.services.repository_browser.stream_command",
        _stream_result(output, calls=stream_calls),
    ):
        view = _browse_location(
            service,
            location_id=location_id,
            snapshot_id="abcdef1234567890",
            path="/",
        )

    assert view["ok"] is True
    assert stream_calls[0]["kwargs"]["env"]["RESTIC_PASSWORD"] == "secret-value"
    entries = cast(list[dict[str, object]], view["entries"])
    names = {entry["name"] for entry in entries}
    assert names == {"data"}
    directory_entry = next(entry for entry in entries if entry["name"] == "data")
    assert directory_entry["browse_url"] == (
        f"/repositories/locations/{location_id}/snapshots/abcdef1234567890/browse" "?path=%2Fdata"
    )
    assert view["breadcrumbs"] == [
        {
            "name": "/",
            "path": "/",
            "browse_url": (
                f"/repositories/locations/{location_id}/snapshots/abcdef1234567890/browse"
                "?path=%2F"
            ),
        }
    ]


def test_browse_location_snapshot_view_inactive_location_uses_donor_credentials(
    tmp_path: Path,
) -> None:
    service, store = _make_service_with_multi_job_config(tmp_path)
    store.persist_refresh(
        job="demo",
        backup="local",
        repository="/repo/secret-value",
        backend_repository_id="repo-id",
        artifacts=[{"id": "abcdef1234567890", "paths": ["/data"]}],
        stats={"mode": "raw-data", "total_size_bytes": 1, "snapshots_count": 1},
        observed_at="2026-06-01T12:00:00+00:00",
    )
    store.persist_refresh(
        job="demo",
        backup="local",
        repository="/repo/orphaned",
        backend_repository_id="repo-id",
        artifacts=[{"id": "abcdef1234567890", "paths": ["/data"]}],
        stats={"mode": "raw-data", "total_size_bytes": 1, "snapshots_count": 1},
        observed_at="2026-06-01T12:01:00+00:00",
    )
    orphaned_location = store.resolve_location("local:/repo/orphaned")
    assert orphaned_location is not None
    location_id = str(orphaned_location["location_id"])

    output = "\n".join(
        [
            '{"message_type": "snapshot", "id": "abcdef1234567890"}',
            (
                '{"message_type": "node", "type": "file", "path": "/data/file.txt", '
                '"size": 12, "mtime": "2026-05-27T00:00:00Z"}'
            ),
        ]
    )
    stream_calls: list[dict[str, Any]] = []
    with patch(
        "src.services.repository_browser.stream_command",
        _stream_result(output, calls=stream_calls),
    ):
        view = _browse_location(
            service,
            location_id=location_id,
            snapshot_id="abcdef1234567890",
            path="/",
        )

    assert view["ok"] is True
    # Donor (demo.local) credentials, but the *orphaned* location's path.
    assert stream_calls[0]["cmd"][2] == "/repo/orphaned"
    assert stream_calls[0]["kwargs"]["env"]["RESTIC_PASSWORD"] == "secret-value"
    entries = cast(list[dict[str, object]], view["entries"])
    assert len(entries) == 1
    assert entries[0]["name"] == "data"
    assert entries[0]["type"] == "dir"


def test_browse_location_snapshot_view_without_donor_returns_no_credentials_error(
    tmp_path: Path,
) -> None:
    """No donor anywhere must short-circuit before any subprocess is invoked."""
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")
    store = RepositoryArtifactStore(db_path=tmp_path / "appdata.db")
    service = RepositoryBrowserService(ConfigService(config_path), store)
    store.persist_refresh(
        job="ghost",
        backup="local",
        repository="/repo/orphaned",
        backend_repository_id="repo-id",
        artifacts=[{"id": "abcdef1234567890", "paths": ["/data"]}],
        stats={"mode": "raw-data", "total_size_bytes": 1, "snapshots_count": 1},
        observed_at="2026-06-01T12:00:00+00:00",
    )
    orphaned_location = store.resolve_location("local:/repo/orphaned")
    assert orphaned_location is not None
    location_id = str(orphaned_location["location_id"])

    with patch(
        "src.services.repository_browser.stream_command",
        AsyncMock(side_effect=AssertionError("no subprocess should be invoked")),
    ):
        view = _browse_location(
            service,
            location_id=location_id,
            snapshot_id="abcdef1234567890",
            path="/",
        )

    assert view["ok"] is False
    assert view["entries"] == []
    assert _error(view)["code"] == "no_credentials"


def test_get_location_snapshots_view_returns_empty_list_without_store(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_browser_config(config_path)
    service = RepositoryBrowserService(ConfigService(config_path))

    with pytest.raises(ServiceError, match="Location not found"):
        asyncio.run(service.get_location_snapshots_view("anything"))
