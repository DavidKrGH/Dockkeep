import asyncio
import gc
import logging
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.job_runner import BackupRunStatsContext
from src.services.backup_stats_collector import BackupStatsCollector
from src.services.errors import ConfigServiceError
from src.services.repository_artifact_store import SCHEMA_VERSION, RepositoryArtifactStore


def _seed_run_step(conn: sqlite3.Connection, run_id: str, run_step_id: str) -> None:
    """Insert a minimal ``runs``+``run_steps`` row pair for FK-checked linking."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT INTO runs (
                run_id, origin, run_kind, job, task_type, task_name, started_at, status, dry_run
            )
            VALUES (?, 'manual', 'job_task', 'job1', 'backup', 'backup1',
                    '2024-06-01T10:00:00Z', 'running', 0)
            """,
            (run_id,),
        )
        conn.execute(
            """
            INSERT INTO run_steps (
                run_step_id, run_id, position, step, backend, task_type, task_name,
                started_at, status, effective_task_config_json
            )
            VALUES (?, ?, 1, 'backup.backup1.backup', 'restic', 'backup', 'backup1',
                    '2024-06-01T10:00:00Z', 'running', '{}')
            """,
            (run_step_id, run_id),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _make_snapshot(
    time: str,
    duration_seconds: float = 10.0,
    snapshot_id: str | None = None,
    **summary_fields: object,
) -> dict[str, object]:
    from datetime import datetime, timedelta, timezone

    dt_start = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    dt_end = dt_start + timedelta(seconds=duration_seconds)
    summary: dict[str, object] = {
        "backup_start": dt_start.isoformat(),
        "backup_end": dt_end.isoformat(),
        "data_added": 1024,
        "files_new": 5,
        "files_changed": 2,
        "files_unmodified": 100,
    }
    summary.update(summary_fields)
    return {
        "id": snapshot_id or time.replace("-", "").replace(":", "").replace(".", ""),
        "time": time,
        "summary": summary,
    }


def _make_collector(
    tmp_path: Path,
    stats_return: dict[str, object] | None = None,
    snapshots_return: list[object] | None = None,
) -> tuple[BackupStatsCollector, RepositoryArtifactStore, MagicMock]:
    store = RepositoryArtifactStore(db_path=tmp_path / "appdata.db")
    repo_service = MagicMock()
    repo_service.backend_repository_id_async = AsyncMock(return_value="repo-id")
    repo_service.stats_async = AsyncMock(
        return_value=stats_return
        or {
            "available": True,
            "repository": "/backups/test",
            "stats": {"total_size": 1073741824, "total_file_count": 5000, "snapshots_count": 10},
        }
    )
    repo_service.snapshots_async = AsyncMock(
        return_value={
            "repository": "/backups/test",
            "snapshots": (
                snapshots_return
                if snapshots_return is not None
                else [_make_snapshot("2024-06-01T10:00:00.000Z")]
            ),
        }
    )
    repo_service.configured_repository_location_keys.return_value = frozenset(
        {"local:/backups/test"}
    )
    collector = BackupStatsCollector(repo_service, store)
    return collector, store, repo_service


def test_full_data_produces_correct_schema(tmp_path: Path) -> None:
    snapshot = _make_snapshot(
        "2024-06-01T10:00:00.000Z",
        duration_seconds=42.6,
        data_added=2048,
        files_new=10,
        files_changed=3,
        files_unmodified=200,
    )
    collector, store, _ = _make_collector(
        tmp_path,
        snapshots_return=[snapshot],
    )
    result = collector.collect_and_store(
        "myjob",
        "mybackup",
        BackupRunStatsContext(
            run_id="run-1",
            backup_summary=snapshot["summary"],
            trigger_kind="backup",
        ),
    )

    assert result is not None
    assert result["schema_version"] == SCHEMA_VERSION
    assert result["job"] == "myjob"
    assert result["backup"] == "mybackup"
    assert result["repository"] == "/backups/test"
    assert result["total_size_bytes"] == 1073741824
    assert result["total_file_count"] == 5000
    assert result["snapshots_count"] == 10

    lb = result["last_backup"]
    assert isinstance(lb, dict)
    assert lb["time"] == "2024-06-01T10:00:00.000000Z"
    assert lb["duration_seconds"] == 43
    assert lb["data_added_bytes"] == 2048
    assert lb["files_new"] == 10
    assert lb["files_changed"] == 3
    assert lb["files_unmodified"] == 200

    snaps = result["snapshots"]
    assert isinstance(snaps, list)
    first = snaps[0]
    assert isinstance(first, dict)
    assert first["time"] == "2024-06-01T10:00:00.000000Z"
    snap_summary = first["summary"]
    assert isinstance(snap_summary, dict)
    assert snap_summary.get("total_duration") is not None

    stored = store.read_backup_view_for_location("myjob", "mybackup", "local:/backups/test")
    assert stored == result


def test_identity_changed_location_triggers_full_snapshot_refresh(tmp_path: Path) -> None:
    collector, store, repo_service = _make_collector(
        tmp_path,
        snapshots_return=[_make_snapshot("2024-06-02T10:00:00.000Z", snapshot_id="new")],
    )
    location_key = "local:/backups/test"
    old_repo_id = store.find_or_create_repository("restic", "old-repo")
    old_location_id = store.upsert_location(old_repo_id, location_key, "/backups/test")
    artifact_id = store.insert_artifact(
        old_repo_id,
        {"id": "old", "time": "2024-06-01T10:00:00.000Z"},
    )
    store.upsert_location_observation(artifact_id, old_location_id)
    repo_service.backend_repository_id_async.return_value = "repo-id"
    repo_service.snapshots_async.side_effect = [
        {
            "repository": "/backups/test",
            "snapshots": [_make_snapshot("2024-06-02T10:00:00.000Z", snapshot_id="new")],
        },
        {
            "repository": "/backups/test",
            "snapshots": [_make_snapshot("2024-06-02T10:00:00.000Z", snapshot_id="new")],
        },
    ]

    result = collector.collect_and_store(
        "job1",
        "backup1",
        BackupRunStatsContext(run_id="run-1"),
    )

    assert result is not None
    assert [snapshot["id"] for snapshot in result["snapshots"]] == ["new"]
    assert repo_service.snapshots_async.call_args_list[0].kwargs["latest"] == 1
    assert repo_service.snapshots_async.call_args_list[1].kwargs["latest"] is None


def test_identity_changed_full_refetch_links_newest_snapshot_not_first_in_list(
    tmp_path: Path,
) -> None:
    """DK-BUG-AUDIT-006: the full re-fetch triggered by an identity change must link
    the chronologically newest snapshot, not whichever snapshot happens to come
    first in restic's (not chronologically guaranteed) response order."""
    older_snapshot = _make_snapshot("2024-06-01T10:00:00.000Z", snapshot_id="older")
    newest_snapshot = _make_snapshot("2024-06-02T10:00:00.000Z", snapshot_id="newest")
    collector, store, repo_service = _make_collector(
        tmp_path,
        snapshots_return=[newest_snapshot],
    )
    location_key = "local:/backups/test"
    old_repo_id = store.find_or_create_repository("restic", "old-repo")
    old_location_id = store.upsert_location(old_repo_id, location_key, "/backups/test")
    artifact_id = store.insert_artifact(
        old_repo_id,
        {"id": "old", "time": "2024-05-01T10:00:00.000Z"},
    )
    store.upsert_location_observation(artifact_id, old_location_id)
    repo_service.backend_repository_id_async.return_value = "repo-id"
    repo_service.snapshots_async.side_effect = [
        {"repository": "/backups/test", "snapshots": [newest_snapshot]},
        {
            "repository": "/backups/test",
            # Older snapshot listed first: the full re-fetch response order
            # after an identity change is not guaranteed to be chronological.
            "snapshots": [older_snapshot, newest_snapshot],
        },
    ]

    with closing(store.connect()) as conn:
        _seed_run_step(conn, "run-1", "step-1")

    collector.collect_and_store(
        "job1",
        "backup1",
        BackupRunStatsContext(run_id="run-1", run_step_id="step-1"),
    )

    with closing(store.connect()) as conn:
        rows = conn.execute("""
            SELECT artifacts.backend_artifact_id
            FROM artifacts
            JOIN run_steps ON run_steps.run_step_id = artifacts.created_run_step_id
            """).fetchall()
    assert [row[0] for row in rows] == ["newest"]


def test_stats_unavailable_still_stores_snapshot_data(tmp_path: Path) -> None:
    collector, store, _ = _make_collector(
        tmp_path,
        stats_return={
            "available": False,
            "repository": "/backups/test",
            "stats": {},
            "error": "restic error",
        },
        snapshots_return=[_make_snapshot("2024-06-01T10:00:00.000Z")],
    )
    result = collector.collect_and_store(
        "job1",
        "backup1",
        BackupRunStatsContext(
            run_id="run-1",
            backup_summary=_make_snapshot("2024-06-01T10:00:00.000Z")["summary"],
        ),
    )

    assert result is not None
    assert result["total_size_bytes"] is None
    assert result["snapshots_count"] == 1
    lb = result["last_backup"]
    assert isinstance(lb, dict)
    assert lb["time"] == "2024-06-01T10:00:00.000000Z"


def test_config_error_while_building_retention_protection_still_stores_data(
    tmp_path: Path,
) -> None:
    collector, store, repo_service = _make_collector(
        tmp_path,
        snapshots_return=[_make_snapshot("2024-06-01T10:00:00.000Z")],
    )
    repo_service.configured_repository_location_keys.side_effect = ConfigServiceError(
        "config_error",
        "invalid config",
    )

    result = collector.collect_and_store(
        "job1",
        "backup1",
        BackupRunStatsContext(run_id="run-1"),
    )

    assert result is not None
    stored = store.read_backup_view_for_location("job1", "backup1", "local:/backups/test")
    assert stored is not None
    assert [snapshot["id"] for snapshot in stored["snapshots"]]
    resolution = store.resolve_location("local:/backups/test")
    assert resolution is not None
    assert store.is_retention_due(str(resolution["repository_id"]))
    repo_service.configured_repository_location_keys.assert_called_once_with()


def test_no_summary_in_snapshots_sets_last_backup_time_only(tmp_path: Path) -> None:
    collector, store, _ = _make_collector(
        tmp_path,
        snapshots_return=[{"id": "snap1", "time": "2024-06-01T10:00:00.000Z"}],
    )
    result = collector.collect_and_store(
        "job1",
        "backup1",
        BackupRunStatsContext(run_id="run-1"),
    )

    assert result is not None
    assert result["last_backup"] is None

    snaps = result["snapshots"]
    assert isinstance(snaps, list)
    assert len(snaps) > 0


def test_empty_snapshots_gives_null_last_backup(tmp_path: Path) -> None:
    collector, store, _ = _make_collector(
        tmp_path,
        stats_return={"available": False, "repository": "/backups/test", "stats": {}},
        snapshots_return=[],
    )
    result = collector.collect_and_store("job1", "backup1")

    assert result is None
    assert store.read_backup_view("job1", "backup1") is None


def test_backup_summary_does_not_create_artifact_when_snapshot_list_is_empty(
    tmp_path: Path,
) -> None:
    summary = _make_snapshot(
        "2024-06-01T10:00:00.000Z",
        duration_seconds=42.5,
        snapshot_id="summary-only",
    )["summary"]
    collector, store, _ = _make_collector(tmp_path, snapshots_return=[])

    with closing(store.connect()) as conn:
        _seed_run_step(conn, "run-created", "step-created")

    result = collector.collect_and_store(
        "job1",
        "backup1",
        BackupRunStatsContext(
            run_id="run-created",
            run_step_id="step-created",
            backup_summary=summary,
        ),
    )

    assert result is not None
    assert result["snapshots"] == []
    with closing(store.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM repository_stats_points WHERE run_step_id = 'step-created'"
            ).fetchone()[0]
            == 1
        )


def test_newest_snapshot_is_selected(tmp_path: Path) -> None:
    newest = _make_snapshot("2024-06-01T00:00:00.000Z", files_new=99)
    collector, store, _ = _make_collector(
        tmp_path,
        snapshots_return=[
            _make_snapshot("2024-01-01T00:00:00.000Z", files_new=1),
            newest,
            _make_snapshot("2024-03-01T00:00:00.000Z", files_new=50),
        ],
    )
    result = collector.collect_and_store(
        "job1",
        "backup1",
        BackupRunStatsContext(
            run_id="run-1",
            backup_summary=newest["summary"],
        ),
    )
    assert result is not None
    lb = result["last_backup"]
    assert isinstance(lb, dict)
    assert lb["files_new"] == 99


def test_exception_in_repo_service_returns_none(tmp_path: Path) -> None:
    store = RepositoryArtifactStore(db_path=tmp_path / "appdata.db")
    repo_service = MagicMock()
    repo_service.backend_repository_id_async = AsyncMock(
        side_effect=RuntimeError("connection refused")
    )
    collector = BackupStatsCollector(repo_service, store)

    result = collector.collect_and_store("job1", "backup1")
    assert result is None
    assert store.read_backup_view("job1", "backup1") is None


@pytest.mark.parametrize(
    "context",
    [
        None,
        BackupRunStatsContext(run_id="run-cancel", run_step_id="step-cancel"),
    ],
)
def test_collect_async_cancellation_during_stats_propagates_without_persist(
    tmp_path: Path,
    context: BackupRunStatsContext | None,
) -> None:
    collector, store, repo_service = _make_collector(tmp_path)
    stats_started = asyncio.Event()
    persist_refresh = MagicMock(wraps=store.persist_refresh)
    persist_backup_run = MagicMock(wraps=store.persist_backup_run)
    store.persist_refresh = persist_refresh
    store.persist_backup_run = persist_backup_run

    async def blocking_stats(*_: object, **__: object) -> dict[str, object]:
        stats_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    repo_service.stats_async.side_effect = blocking_stats

    async def scenario() -> None:
        task = asyncio.create_task(collector.collect_and_store_async("job1", "backup1", context))
        await stats_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.5)

    asyncio.run(scenario())

    persist_refresh.assert_not_called()
    persist_backup_run.assert_not_called()


def test_collect_serializes_concurrent_collects_across_event_loops(tmp_path: Path) -> None:
    """Two threads with separate event loops sharing one collector both persist.

    Mirrors the GUI-mode topology: the FastAPI loop and the scheduler thread's
    loop share the same collector instance. Both concurrent collects for the
    same job.backup must be serialized and succeed.
    """
    collector, store, _ = _make_collector(tmp_path)
    first_inside_lock = threading.Event()
    release_first = threading.Event()
    original_resolve = store.resolve_observed_location
    resolve_calls: list[int] = []

    def blocking_resolve(**kwargs: object) -> dict[str, object]:
        resolve_calls.append(1)
        if len(resolve_calls) == 1:
            first_inside_lock.set()
            assert release_first.wait(timeout=5)
        return original_resolve(**kwargs)

    store.resolve_observed_location = blocking_resolve

    results: dict[str, dict[str, object] | None] = {}

    def run_collect(slot: str) -> None:
        results[slot] = collector.collect_and_store("job1", "backup1")

    first = threading.Thread(target=run_collect, args=("first",))
    second = threading.Thread(target=run_collect, args=("second",))
    first.start()
    assert first_inside_lock.wait(timeout=5)
    second.start()
    time.sleep(0.2)  # let the second thread reach the contended backup lock
    release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)
    assert not first.is_alive() and not second.is_alive()
    assert results["first"] is not None
    assert results["second"] is not None


def test_backup_locks_are_strongly_referenced_between_uses(tmp_path: Path) -> None:
    collector, _, _ = _make_collector(tmp_path)

    first_lock = collector._backup_lock("job1", "backup1")
    first_lock_id = id(first_lock)
    del first_lock
    gc.collect()

    assert id(collector._backup_lock("job1", "backup1")) == first_lock_id


def test_stats_timeout_env_is_forwarded_to_restic_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DK_STATS_TIMEOUT", "75")
    collector, _, repo_service = _make_collector(tmp_path)

    collector.collect_and_store("job1", "backup1")

    repo_service.backend_repository_id_async.assert_called_once_with(
        "job1",
        "backup1",
        timeout=75,
        timeout_env_hint="DK_STATS_TIMEOUT",
    )
    repo_service.stats_async.assert_called_once_with(
        "job1",
        "backup1",
        mode="raw-data",
        timeout=75,
        timeout_env_hint="DK_STATS_TIMEOUT",
    )
    repo_service.snapshots_async.assert_called_once_with(
        "job1",
        "backup1",
        latest=None,
        timeout=75,
        timeout_env_hint="DK_STATS_TIMEOUT",
    )


def test_stats_timeout_log_mentions_editable_env(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    collector, _, repo_service = _make_collector(
        tmp_path,
        stats_return={
            "available": False,
            "repository": "/backups/test",
            "stats": {},
            "error": "restic command timed out. Adjust DK_STATS_TIMEOUT to change this timeout.",
        },
    )

    with caplog.at_level(logging.WARNING):
        collector.collect_and_store("job1", "backup1")

    assert any("DK_STATS_TIMEOUT" in record.message for record in caplog.records)


def test_backup_run_uses_latest_snapshot_for_artifact_identity(
    tmp_path: Path,
) -> None:
    collector, _store, repo_service = _make_collector(tmp_path)

    collector.collect_and_store(
        "job1",
        "backup1",
        BackupRunStatsContext(run_id="run-1"),
    )

    repo_service.snapshots_async.assert_called_once_with(
        "job1",
        "backup1",
        latest=1,
        timeout=600,
        timeout_env_hint="DK_STATS_TIMEOUT",
    )


def test_successful_run_with_existing_snapshots_uses_latest_one(tmp_path: Path) -> None:
    collector, store, repo_service = _make_collector(
        tmp_path,
        snapshots_return=[_make_snapshot("2024-06-02T10:00:00.000Z", snapshot_id="snap2")],
    )
    store.persist_refresh(
        job="job1",
        backup="backup1",
        repository="/backups/test",
        backend_repository_id="repo-id",
        artifacts=[_make_snapshot("2024-06-01T10:00:00.000Z", snapshot_id="snap1")],
        stats={"mode": "raw-data", "total_size_bytes": 1, "snapshots_count": 1},
    )

    collector.collect_and_store(
        "job1",
        "backup1",
        BackupRunStatsContext(run_id="run-2"),
    )

    repo_service.snapshots_async.assert_called_once_with(
        "job1",
        "backup1",
        latest=1,
        timeout=600,
        timeout_env_hint="DK_STATS_TIMEOUT",
    )


def test_cleanup_writes_stats_point_without_snapshot_reconcile(
    tmp_path: Path,
) -> None:
    collector, store, repo_service = _make_collector(
        tmp_path,
        snapshots_return=[_make_snapshot("2024-06-02T10:00:00.000Z", snapshot_id="snap2")],
    )
    store.persist_refresh(
        job="job1",
        backup="backup1",
        repository="/backups/test",
        backend_repository_id="repo-id",
        artifacts=[_make_snapshot("2024-06-01T10:00:00.000Z", snapshot_id="snap1")],
        stats={"mode": "raw-data", "total_size_bytes": 1, "snapshots_count": 1},
    )

    collector.collect_and_store(
        "job1",
        "backup1",
        BackupRunStatsContext(run_id="run-3", trigger_kind="cleanup"),
    )

    repo_service.snapshots_async.assert_not_called()
    stored = store.read_backup_view_for_location("job1", "backup1", "local:/backups/test")
    assert stored is not None
    assert [snapshot["id"] for snapshot in stored["snapshots"]] == ["snap1"]
    with closing(store.connect()) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM repository_stats_points WHERE trigger = 'cleanup'"
            ).fetchone()[0]
            == 1
        )


def test_failed_snapshot_refresh_keeps_existing_snapshots_present(tmp_path: Path) -> None:
    store = RepositoryArtifactStore(db_path=tmp_path / "appdata.db")
    repo_service = MagicMock()
    repo_service.backend_repository_id_async = AsyncMock(return_value="repo-id")
    repo_service.stats_async = AsyncMock(
        return_value={
            "available": True,
            "repository": "/backups/test",
            "stats": {"total_size": 1073741824, "total_file_count": 5000, "snapshots_count": 10},
        }
    )
    repo_service.snapshots_async = AsyncMock(
        return_value={
            "job": "job1",
            "backup": "backup1",
            "repository": "/backups/test",
            "snapshots": [],
            "error": "timeout",
        }
    )
    collector = BackupStatsCollector(repo_service, store)

    store.persist_refresh(
        job="job1",
        backup="backup1",
        repository="/backups/test",
        backend_repository_id="repo-id",
        artifacts=[_make_snapshot("2024-06-01T10:00:00.000Z", snapshot_id="snap1")],
        stats={"mode": "raw-data", "total_size_bytes": 1, "snapshots_count": 1},
    )

    # Full-refresh path (context=None) with a failed snapshot query must NOT
    # mark the existing present snapshot as removed.
    collector.collect_and_store("job1", "backup1")

    repo_service.snapshots_async.assert_called_once_with(
        "job1",
        "backup1",
        latest=None,
        timeout=600,
        timeout_env_hint="DK_STATS_TIMEOUT",
    )
    stored = store.read_backup_view("job1", "backup1")
    assert stored is not None
    assert [snapshot["id"] for snapshot in stored["snapshots"]] == ["snap1"]
    assert stored["snapshots"][0]["present"] is True

    with closing(store.connect()) as conn:
        row = conn.execute(
            """
            SELECT obs.present, obs.removed_at, obs.removed_by_run_step_id
            FROM artifact_locations AS obs
            JOIN artifacts AS artifacts
              ON artifacts.artifact_id = obs.artifact_id
            WHERE artifacts.backend_artifact_id = ?
            """,
            ("snap1",),
        ).fetchone()
    assert row["present"] == 1
    assert row["removed_at"] is None
    assert row["removed_by_run_step_id"] is None


def test_failed_snapshot_retention_run_keeps_existing_snapshots_present(tmp_path: Path) -> None:
    collector, store, repo_service = _make_collector(
        tmp_path,
        snapshots_return=[
            _make_snapshot("2024-06-01T10:00:00.000Z", snapshot_id="snap1"),
            _make_snapshot("2024-06-02T10:00:00.000Z", snapshot_id="snap2"),
        ],
    )

    collector.collect_and_store("job1", "backup1", BackupRunStatsContext(run_id="run-1"))
    seeded = store.read_backup_view("job1", "backup1")
    assert {snapshot["id"] for snapshot in seeded["snapshots"]} == {"snap1", "snap2"}

    # A retention/cleanup run needs the full snapshot list, but restic snapshots fails.
    # The reconcile must NOT run against the failed (empty) list and wrongly mark
    # the existing snapshots as removed. Retention runs never write a stats point,
    # so the full-reconcile is the only persisted effect of this run.
    repo_service.snapshots_async.return_value = {
        "repository": "/backups/test",
        "snapshots": [],
        "error": "timeout",
    }
    collector.collect_and_store(
        "job1",
        "backup1",
        BackupRunStatsContext(run_id="run-2", trigger_kind="retention"),
    )

    stored = store.read_backup_view("job1", "backup1")
    assert {snapshot["id"] for snapshot in stored["snapshots"]} == {"snap1", "snap2"}
    assert all(snapshot["present"] is True for snapshot in stored["snapshots"])

    with closing(store.connect()) as conn:
        present = conn.execute(
            "SELECT COUNT(*) AS n FROM artifact_locations WHERE present = 1"
        ).fetchone()["n"]
        retention_points = conn.execute(
            "SELECT COUNT(*) AS n FROM repository_stats_points WHERE trigger = 'retention'"
        ).fetchone()["n"]
    assert present == 2
    assert retention_points == 0


def test_explicit_refresh_uses_full_snapshot_list_and_does_not_record_dk_run(
    tmp_path: Path,
) -> None:
    collector, store, repo_service = _make_collector(
        tmp_path,
        snapshots_return=[_make_snapshot("2024-06-02T10:00:00.000Z", snapshot_id="snap2")],
    )
    store.persist_refresh(
        job="job1",
        backup="backup1",
        repository="/backups/test",
        backend_repository_id="repo-id",
        artifacts=[_make_snapshot("2024-06-01T10:00:00.000Z", snapshot_id="snap1")],
        stats={"mode": "raw-data", "total_size_bytes": 1, "snapshots_count": 1},
    )

    collector.collect_and_store("job1", "backup1")

    repo_service.snapshots_async.assert_called_once_with(
        "job1",
        "backup1",
        latest=None,
        timeout=600,
        timeout_env_hint="DK_STATS_TIMEOUT",
    )
    # Refresh stats points are never linked to a run step.
    with closing(store.connect()) as conn:
        unlinked = conn.execute(
            "SELECT COUNT(*) FROM repository_stats_points "
            "WHERE trigger = 'refresh' AND run_step_id IS NULL"
        ).fetchone()[0]
        linked = conn.execute(
            "SELECT COUNT(*) FROM repository_stats_points WHERE run_step_id IS NOT NULL"
        ).fetchone()[0]
    assert unlinked == 2
    assert linked == 0


def test_backup_artifact_identity_comes_from_latest_snapshot_not_summary_snapshot_id(
    tmp_path: Path,
) -> None:
    summary = _make_snapshot(
        "2024-06-01T10:00:00.000Z",
        snapshot_id="summary-id",
        duration_seconds=11,
    )["summary"]
    summary["snapshot_id"] = "summary-id"
    collector, store, _repo_service = _make_collector(
        tmp_path,
        snapshots_return=[_make_snapshot("2024-06-02T10:00:00.000Z", snapshot_id="snap2")],
    )

    with closing(store.connect()) as conn:
        _seed_run_step(conn, "run-latest", "step-latest")

    collector.collect_and_store(
        "job1",
        "backup1",
        BackupRunStatsContext(
            run_id="run-latest",
            run_step_id="step-latest",
            backup_summary=summary,
        ),
    )

    with closing(store.connect()) as conn:
        rows = conn.execute("""
            SELECT artifacts.backend_artifact_id, artifacts.raw_backup_output_json,
                   run_steps.run_step_id, run_steps.run_id
            FROM artifacts
            JOIN run_steps
              ON run_steps.run_step_id = artifacts.created_run_step_id
            """).fetchall()
    assert [(row[0], row[2], row[3]) for row in rows] == [("snap2", "step-latest", "run-latest")]
    assert '"snapshot_id":"summary-id"' in rows[0][1]
