import concurrent.futures
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from src.core.job_runner import BackupRunStatsContext
from src.services.repository_artifact_store import (
    REPOSITORY_IDENTITY_CHANGED,
    RepositoryArtifactStore,
    find_or_create_repository,
    repository_location_key,
)

SCHEMA_TABLES = {
    "repositories",
    "repository_locations",
    "artifacts",
    "artifact_locations",
    "repository_stats_points",
    "run_steps",
    "run_restores",
}


@pytest.fixture()
def store(tmp_path: Path) -> RepositoryArtifactStore:
    return RepositoryArtifactStore(db_path=tmp_path / "appdata.db")


def _snapshot(
    backend_artifact_id: str,
    *,
    created_at: str = "2026-06-01T12:00:00+00:00",
    paths: list[str] | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "backend_artifact_id": backend_artifact_id,
        "backend_artifact_short_id": backend_artifact_id[:8],
        "created_at": created_at,
        "metadata": {
            "tree": f"tree-{backend_artifact_id}",
            "hostname": "host1",
            "username": "user1",
            "uid": 1000,
            "gid": 1000,
            "program_version": "restic 0.18.0",
            "raw_metadata": {"id": backend_artifact_id, "time": created_at},
        },
        "summary": {
            "backup_start": created_at,
            "backup_end": "2026-01-01T12:00:42+00:00",
            "total_duration_seconds": 42.0,
            "files_new": 1,
            "files_changed": 2,
            "files_unmodified": 3,
            "dirs_new": 4,
            "dirs_changed": 5,
            "dirs_unmodified": 6,
            "data_blobs": 7,
            "tree_blobs": 8,
            "data_added": 1024,
            "data_added_packed": 900,
            "total_files_processed": 10,
            "total_bytes_processed": 2048,
            "raw_summary": {"message_type": "summary", "snapshot_id": backend_artifact_id},
        },
        "paths": paths or ["/data"],
        "tags": tags or ["daily"],
    }


def _stats(
    *,
    collected_at: str = "2026-06-01T12:01:00+00:00",
    total_size_bytes: int = 4096,
) -> dict[str, Any]:
    return {
        "collected_at": collected_at,
        "mode": "raw-data",
        "total_size_bytes": total_size_bytes,
        "total_file_count": 12,
        "artifacts_count": 1,
        "raw_stats": {"total_size": total_size_bytes, "total_file_count": 12},
    }


def _insert_run_step(store: RepositoryArtifactStore, run_step_id: str = "step-1") -> None:
    with closing(store.connect()) as conn:
        conn.execute("""
            INSERT INTO runs (
                run_id, origin, run_kind, job, task_type, task_name,
                started_at, finished_at, status, error, dry_run
            )
            VALUES (
                'run-1', 'manual', 'job_task', 'job1', 'backup', 'main',
                '2026-06-01T21:59:00.000000Z', NULL, 'running', NULL, 0
            )
            """)
        conn.execute(
            """
            INSERT INTO run_steps (
                run_step_id, run_id, position, step, backend, task_type, task_name,
                started_at, finished_at, status, error, effective_task_config_json
            )
            VALUES (
                ?, 'run-1', 0, 'backup.main.backup', 'restic', 'backup', 'main',
                '2026-06-01T21:59:00.000000Z', NULL, 'running', NULL, '{}'
            )
            """,
            (run_step_id,),
        )


def _table_names(db_path: Path) -> set[str]:
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {str(row[0]) for row in rows}


def test_schema_init_creates_repository_artifact_tables(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "appdata.db"
    store = RepositoryArtifactStore(db_path=db_path)
    store.find_or_create_repository(
        backend="restic",
        backend_repository_id="repo-1",
        first_seen_at="2026-01-01T00:00:00+00:00",
    )

    assert SCHEMA_TABLES <= _table_names(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_new_connections_enable_foreign_keys_and_busy_timeout(
    store: RepositoryArtifactStore,
) -> None:
    with closing(store.connect()) as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000


def test_repository_location_key_normalizes_direct_and_restic_rclone_forms() -> None:
    assert repository_location_key("rclone:remote:path") == repository_location_key("remote:path")


def test_find_location_resolves_metadata_by_location_id_alone(
    store: RepositoryArtifactStore,
) -> None:
    repo_id = store.find_or_create_repository("restic", "repo-1")
    location_id = store.upsert_location(repo_id, repository_location_key("/repo"), "/repo")

    found = store.find_location(location_id)

    assert found is not None
    assert found["location_id"] == location_id
    assert found["repository_id"] == repo_id
    assert found["backend"] == "restic"
    assert found["backend_repository_id"] == "repo-1"
    assert found["repository_location_key"] == repository_location_key("/repo")
    assert found["display_repository"] == "/repo"


def test_find_location_returns_none_for_unknown_location_id(
    store: RepositoryArtifactStore,
) -> None:
    assert store.find_location("does-not-exist") is None


def test_resolve_observed_location_rechecks_backend_identity(
    store: RepositoryArtifactStore,
) -> None:
    location_key = repository_location_key("/repo")
    repo_id = store.find_or_create_repository("restic", "repo-1")
    location_id = store.upsert_location(repo_id, location_key, "/repo")

    result = store.resolve_observed_location(
        backend="restic",
        backend_repository_id="repo-1",
        repository_location_key=location_key,
        display_repository="/repo",
    )

    assert result["status"] == "known_location"
    assert result["repository_id"] == repo_id
    assert result["location_id"] == location_id


def test_resolve_observed_location_attaches_new_location_to_known_repository(
    store: RepositoryArtifactStore,
) -> None:
    repo_id = store.find_or_create_repository("restic", "repo-1")
    result = store.resolve_observed_location(
        backend="restic",
        backend_repository_id="repo-1",
        repository_location_key=repository_location_key("/repo-copy"),
        display_repository="/repo-copy",
    )

    assert result["status"] == "new_location"
    assert result["repository_id"] == repo_id


def test_resolve_observed_location_rebinds_identity_changed_location(
    store: RepositoryArtifactStore,
) -> None:
    location_key = repository_location_key("/repo")
    assert store.persist_refresh(
        job="job1",
        backup="main",
        backend="restic",
        backend_repository_id="old-repo",
        repository_location_key=location_key,
        display_repository="/repo",
        artifacts=[_snapshot("old-snap")],
        stats=_stats(),
    )
    old_location = store.resolve_location(location_key)
    assert old_location is not None
    old_repo_id = str(old_location["repository_id"])

    result = store.resolve_observed_location(
        backend="restic",
        backend_repository_id="new-repo",
        repository_location_key=location_key,
        display_repository="/repo",
    )

    assert result["status"] == REPOSITORY_IDENTITY_CHANGED
    assert result["previous_repository_id"] == old_repo_id
    assert result["repository_id"] != old_repo_id
    with closing(store.connect()) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM artifacts WHERE repository_id = ?",
                (old_repo_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM repository_stats_points WHERE repository_id = ?",
                (old_repo_id,),
            ).fetchone()[0]
            == 0
        )
        rebound = conn.execute(
            """
            SELECT repositories.backend_repository_id
            FROM repository_locations
            JOIN repositories USING (repository_id)
            WHERE repository_location_key = ?
            """,
            (location_key,),
        ).fetchone()
    assert rebound[0] == "new-repo"


def test_resolve_observed_location_rebind_does_not_delete_runs_or_run_steps(
    store: RepositoryArtifactStore,
) -> None:
    location_key = repository_location_key("/repo")
    old_repo_id = store.find_or_create_repository("restic", "old-repo")
    old_location_id = store.upsert_location(old_repo_id, location_key, "/repo")
    with closing(store.connect()) as conn:
        conn.executescript("""
            INSERT INTO runs (
                run_id, origin, run_kind, job, task_type, task_name,
                started_at, finished_at, status, error, dry_run
            )
            VALUES (
                'run-1', 'manual', 'job_task', 'job1', 'backup', 'main',
                '2026-01-01T00:00:00Z', '2026-01-01T00:01:00Z', 'success', NULL, 0
            );

            INSERT INTO run_steps (
                run_step_id, run_id, position, step, backend, task_type, task_name,
                started_at, finished_at, status, error, effective_task_config_json
            )
            VALUES (
                'step-1', 'run-1', 0, 'backup.main.backup', 'restic', 'backup', 'main',
                '2026-01-01T00:00:00Z', '2026-01-01T00:01:00Z', 'success', NULL, '{}'
            );
            """)
        conn.commit()
    artifact_id = store.insert_artifact(old_repo_id, _snapshot("old-snap"))
    with closing(store.connect()) as conn:
        conn.execute(
            "UPDATE artifacts SET created_run_step_id = 'step-1' WHERE artifact_id = ?",
            (artifact_id,),
        )
        conn.commit()
    store.upsert_location_observation(artifact_id, old_location_id)

    result = store.resolve_observed_location(
        backend="restic",
        backend_repository_id="new-repo",
        repository_location_key=location_key,
        display_repository="/repo",
    )

    assert result["status"] == REPOSITORY_IDENTITY_CHANGED
    with closing(store.connect()) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM artifacts WHERE repository_id = ?", (old_repo_id,)
            ).fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM runs WHERE run_id = 'run-1'").fetchone()[0] == 1
        assert (
            conn.execute("SELECT COUNT(*) FROM run_steps WHERE run_step_id = 'step-1'").fetchone()[
                0
            ]
            == 1
        )


def test_write_primitives_are_idempotent_and_preserve_insert_only_data(
    store: RepositoryArtifactStore,
) -> None:
    repo_id = store.find_or_create_repository(
        backend="restic",
        backend_repository_id="repo-1",
        first_seen_at="2026-01-01T00:00:00+00:00",
    )
    assert (
        store.find_or_create_repository(
            backend="restic",
            backend_repository_id="repo-1",
            first_seen_at="2026-01-02T00:00:00+00:00",
        )
        == repo_id
    )
    location_id = store.upsert_location(
        repository_id=repo_id,
        repository_location_key="local:/repo",
        display_repository="/repo",
        seen_at="2026-01-01T00:00:00+00:00",
        successful=True,
    )
    artifact_id = store.insert_artifact(
        repository_id=repo_id,
        artifact=_snapshot("snap-1", paths=["/original"], tags=["daily"]),
        first_seen_at="2026-01-01T00:01:00+00:00",
    )
    store.insert_artifact(
        repository_id=repo_id,
        artifact=_snapshot("snap-1", paths=["/changed"], tags=["changed"]),
        first_seen_at="2026-01-02T00:01:00+00:00",
    )
    store.upsert_location_observation(
        artifact_id=artifact_id,
        location_id=location_id,
        seen_at="2026-01-01T00:02:00+00:00",
        present=True,
    )

    present_artifacts = store.list_present_artifacts(repo_id, location_id=location_id)
    detail = next(
        artifact for artifact in present_artifacts if artifact["artifact_id"] == artifact_id
    )

    assert detail["artifact_id"] == artifact_id
    assert detail["backend_artifact_id"] == "snap-1"
    assert detail["paths"] == ["/original"]
    assert detail["tags"] == ["daily"]
    assert detail["metadata"]["hostname"] == "host1"
    assert detail["summary"]["files_new"] == 1
    assert any(artifact["artifact_id"] == artifact_id for artifact in present_artifacts)


def test_backup_summary_diagnostics_do_not_fill_canonical_artifact_fields(
    store: RepositoryArtifactStore,
) -> None:
    snapshot = _snapshot("snap-1")
    snapshot.pop("summary")
    summary = {
        "message_type": "summary",
        "snapshot_id": "snap-1",
        "backup_start": "2026-06-01T12:00:00+00:00",
        "backup_end": "2026-06-01T12:00:42+00:00",
        "total_duration": 42.0,
    }

    assert store.persist_backup_run(
        job="job1",
        backup="main",
        backend="restic",
        backend_repository_id="repo-1",
        repository_location_key="local:/repo",
        display_repository="/repo",
        artifacts=[snapshot],
        context=BackupRunStatsContext(backup_summary=summary, trigger_kind="backup"),
    )

    with closing(store.connect()) as conn:
        row = conn.execute("""
            SELECT backup_start, backup_end, duration_seconds, raw_backup_output_json
            FROM artifacts
            WHERE backend_artifact_id = 'snap-1'
            """).fetchone()
    assert row["backup_start"] is None
    assert row["backup_end"] is None
    assert row["duration_seconds"] is None
    assert '"message_type":"summary"' in row["raw_backup_output_json"]


def test_persist_backup_run_is_atomic_and_non_fatal_on_db_error(
    store: RepositoryArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_insert_stats_point = store.insert_stats_point

    def fail_after_artifact_writes(*args: Any, **kwargs: Any) -> str:
        original_insert_stats_point(*args, **kwargs)
        raise sqlite3.OperationalError("simulated write failure")

    monkeypatch.setattr(store, "insert_stats_point", fail_after_artifact_writes)

    result = store.persist_backup_run(
        job="job1",
        backup="main",
        backend="restic",
        backend_repository_id="repo-1",
        repository_location_key="local:/repo",
        display_repository="/repo",
        artifacts=[_snapshot("snap-1")],
        stats=_stats(),
        context=BackupRunStatsContext(run_step_id="step-1", trigger_kind="backup"),
    )

    assert result is False
    assert store.resolve_location("local:/repo") is None
    with closing(sqlite3.connect(store.db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM repository_stats_points").fetchone()[0] == 0


def test_persist_backup_run_normalizes_snapshot_times_before_newest_selection(
    store: RepositoryArtifactStore,
) -> None:
    _insert_run_step(store, "step-newest")
    lexically_later_but_older = _snapshot("old-snapshot", created_at="2026-06-01T23:30:00+02:00")
    chronologically_newer = _snapshot("new-snapshot", created_at="2026-06-01T22:00:00Z")

    assert store.persist_backup_run(
        job="job1",
        backup="main",
        backend="restic",
        backend_repository_id="repo-1",
        repository_location_key="local:/repo",
        display_repository="/repo",
        artifacts=[lexically_later_but_older, chronologically_newer],
        context=BackupRunStatsContext(
            run_step_id="step-newest",
            backup_summary=chronologically_newer["summary"],
            trigger_kind="backup",
        ),
        stats=_stats(collected_at="2026-06-01T22:00:01Z"),
        observed_at="2026-06-01T22:00:02Z",
    )

    with closing(sqlite3.connect(store.db_path)) as conn:
        rows = conn.execute("""
            SELECT backend_artifact_id, created_at, created_run_step_id
            FROM artifacts
            ORDER BY backend_artifact_id
            """).fetchall()

    assert rows == [
        ("new-snapshot", "2026-06-01T22:00:00.000000Z", "step-newest"),
        ("old-snapshot", "2026-06-01T21:30:00.000000Z", None),
    ]


def test_persist_refresh_marks_missing_artifacts_removed_only_for_same_location(
    store: RepositoryArtifactStore,
) -> None:
    assert store.persist_refresh(
        job="job1",
        backup="main",
        backend="restic",
        backend_repository_id="repo-1",
        repository_location_key="local:/repo-a",
        display_repository="/repo-a",
        artifacts=[_snapshot("snap-1"), _snapshot("snap-2")],
        stats=_stats(),
    )
    assert store.persist_refresh(
        job="job2",
        backup="main",
        backend="restic",
        backend_repository_id="repo-1",
        repository_location_key="local:/repo-b",
        display_repository="/repo-b",
        artifacts=[_snapshot("snap-1")],
        stats=_stats(collected_at="2026-06-01T12:02:00+00:00"),
    )

    assert store.persist_refresh(
        job="job1",
        backup="main",
        backend="restic",
        backend_repository_id="repo-1",
        repository_location_key="local:/repo-a",
        display_repository="/repo-a",
        artifacts=[_snapshot("snap-1")],
        stats=_stats(collected_at="2026-06-01T12:03:00+00:00"),
    )

    repo = store.resolve_location("local:/repo-a")
    assert repo is not None
    present_for_a = store.list_present_artifacts(
        repository_id=repo["repository_id"],
        location_id=repo["location_id"],
    )
    assert [artifact["backend_artifact_id"] for artifact in present_for_a] == ["snap-1"]

    repo_b = store.resolve_location("local:/repo-b")
    assert repo_b is not None
    present_for_b = store.list_present_artifacts(
        repository_id=repo_b["repository_id"],
        location_id=repo_b["location_id"],
    )
    assert [artifact["backend_artifact_id"] for artifact in present_for_b] == ["snap-1"]


def test_list_repositories_returns_empty_list_when_no_repositories_exist(
    store: RepositoryArtifactStore,
) -> None:
    assert store.list_repositories() == []


def test_list_repositories_attaches_all_locations_to_their_repository(
    store: RepositoryArtifactStore,
) -> None:
    assert store.persist_refresh(
        job="job1",
        backup="main",
        backend="restic",
        backend_repository_id="repo-1",
        repository_location_key="local:/repo-a",
        display_repository="/repo-a",
        artifacts=[_snapshot("snap-a")],
        stats=_stats(),
    )
    assert store.persist_refresh(
        job="job2",
        backup="main",
        backend="restic",
        backend_repository_id="repo-1",
        repository_location_key="local:/repo-b",
        display_repository="/repo-b",
        artifacts=[_snapshot("snap-b")],
        stats=_stats(collected_at="2026-06-01T12:02:00+00:00"),
    )

    repositories = store.list_repositories()

    assert len(repositories) == 1
    repository = repositories[0]
    assert repository["backend_repository_id"] == "repo-1"
    assert sorted(location["display_repository"] for location in repository["locations"]) == [
        "/repo-a",
        "/repo-b",
    ]


def test_list_repositories_distinguishes_multiple_repository_identities(
    store: RepositoryArtifactStore,
) -> None:
    assert store.persist_refresh(
        job="job1",
        backup="main",
        backend="restic",
        backend_repository_id="repo-1",
        repository_location_key="local:/repo-1",
        display_repository="/repo-1",
        artifacts=[_snapshot("snap-1")],
        stats=_stats(),
    )
    assert store.persist_refresh(
        job="job2",
        backup="main",
        backend="restic",
        backend_repository_id="repo-2",
        repository_location_key="local:/repo-2",
        display_repository="/repo-2",
        artifacts=[_snapshot("snap-2")],
        stats=_stats(collected_at="2026-06-01T12:02:00+00:00"),
    )

    repositories = store.list_repositories()

    assert sorted(repository["backend_repository_id"] for repository in repositories) == [
        "repo-1",
        "repo-2",
    ]
    by_backend_id = {repository["backend_repository_id"]: repository for repository in repositories}
    assert [
        location["display_repository"] for location in by_backend_id["repo-1"]["locations"]
    ] == ["/repo-1"]
    assert [
        location["display_repository"] for location in by_backend_id["repo-2"]["locations"]
    ] == ["/repo-2"]


def test_delete_location_cascades_its_history_but_keeps_other_locations(
    store: RepositoryArtifactStore,
) -> None:
    assert store.persist_refresh(
        job="job1",
        backup="main",
        backend="restic",
        backend_repository_id="repo-1",
        repository_location_key="local:/repo-a",
        display_repository="/repo-a",
        artifacts=[_snapshot("snap-a")],
        stats=_stats(total_size_bytes=1024),
    )
    assert store.persist_refresh(
        job="job2",
        backup="main",
        backend="restic",
        backend_repository_id="repo-1",
        repository_location_key="local:/repo-b",
        display_repository="/repo-b",
        artifacts=[_snapshot("snap-b")],
        stats=_stats(collected_at="2026-06-01T12:02:00+00:00", total_size_bytes=2048),
    )
    repo_a = store.resolve_location("local:/repo-a")
    repo_b = store.resolve_location("local:/repo-b")
    assert repo_a is not None
    assert repo_b is not None
    assert [
        point["total_size_bytes"]
        for point in store.growth_points(
            str(repo_a["repository_id"]), location_id=str(repo_a["location_id"])
        )
    ] == [1024]
    assert [
        point["total_size_bytes"]
        for point in store.growth_points(
            str(repo_b["repository_id"]), location_id=str(repo_b["location_id"])
        )
    ] == [2048]

    assert store.delete_location(str(repo_a["repository_id"]), str(repo_a["location_id"])) is True

    assert store.resolve_location("local:/repo-a") is None
    assert store.read_backup_view_for_location("job1", "main", "local:/repo-a") is None
    assert (
        store.list_present_artifacts(
            str(repo_a["repository_id"]),
            location_id=str(repo_a["location_id"]),
        )
        == []
    )
    remaining = store.list_present_artifacts(
        str(repo_b["repository_id"]),
        location_id=str(repo_b["location_id"]),
    )
    assert [artifact["backend_artifact_id"] for artifact in remaining] == ["snap-b"]
    growth_sizes = [
        point["total_size_bytes"] for point in store.growth_points(str(repo_a["repository_id"]))
    ]
    assert growth_sizes == [2048]


def test_prune_locations_by_count_deletes_oldest_first(
    store: RepositoryArtifactStore,
) -> None:
    repo_id = None
    for suffix in ("a", "b", "c"):
        assert store.persist_refresh(
            job="job1",
            backup=f"backup-{suffix}",
            backend="restic",
            backend_repository_id="repo-1",
            repository_location_key=f"local:/repo-{suffix}",
            display_repository=f"/repo-{suffix}",
            artifacts=[],
            stats=None,
            observed_at="2026-06-01T12:00:00+00:00",
        )
        resolution = store.resolve_location(f"local:/repo-{suffix}")
        assert resolution is not None
        repo_id = str(resolution["repository_id"])
    assert repo_id is not None

    store.prune_locations_by_count(repo_id, limit=2)

    assert store.resolve_location("local:/repo-a") is None
    assert store.resolve_location("local:/repo-b") is not None
    assert store.resolve_location("local:/repo-c") is not None


def test_prune_locations_by_count_never_deletes_config_referenced_locations(
    store: RepositoryArtifactStore,
) -> None:
    repo_id = None
    for suffix in ("a", "b", "c"):
        assert store.persist_refresh(
            job="job1",
            backup=f"backup-{suffix}",
            backend="restic",
            backend_repository_id="repo-1",
            repository_location_key=f"local:/repo-{suffix}",
            display_repository=f"/repo-{suffix}",
            artifacts=[],
            stats=None,
            observed_at="2026-06-01T12:00:00+00:00",
        )
        resolution = store.resolve_location(f"local:/repo-{suffix}")
        assert resolution is not None
        repo_id = str(resolution["repository_id"])
    assert repo_id is not None

    store.prune_locations_by_count(
        repo_id, limit=0, protected_location_keys=frozenset({"local:/repo-a"})
    )

    assert store.resolve_location("local:/repo-a") is not None
    assert store.resolve_location("local:/repo-b") is None
    assert store.resolve_location("local:/repo-c") is None


def test_merge_location_moves_observations_and_stats_to_target(
    store: RepositoryArtifactStore,
) -> None:
    assert store.persist_refresh(
        job="job1",
        backup="main",
        backend="restic",
        backend_repository_id="repo-1",
        repository_location_key="local:/repo-a",
        display_repository="/repo-a",
        artifacts=[_snapshot("shared"), _snapshot("source-only")],
        stats=_stats(total_size_bytes=1024),
        observed_at="2026-06-01T12:00:00+00:00",
    )
    assert store.persist_refresh(
        job="job1",
        backup="main",
        backend="restic",
        backend_repository_id="repo-1",
        repository_location_key="local:/repo-b",
        display_repository="/repo-b",
        artifacts=[_snapshot("shared"), _snapshot("target-only")],
        stats=_stats(collected_at="2026-06-01T12:02:00+00:00", total_size_bytes=2048),
        observed_at="2026-06-01T12:02:00+00:00",
    )
    source = store.resolve_location("local:/repo-a")
    target = store.resolve_location("local:/repo-b")
    assert source is not None
    assert target is not None

    assert (
        store.merge_location(
            str(source["repository_id"]),
            str(source["location_id"]),
            str(target["location_id"]),
        )
        is True
    )

    assert store.resolve_location("local:/repo-a") is None
    artifacts = store.list_present_artifacts(
        str(target["repository_id"]), location_id=str(target["location_id"])
    )
    assert sorted(artifact["backend_artifact_id"] for artifact in artifacts) == [
        "shared",
        "source-only",
        "target-only",
    ]
    with closing(store.connect()) as conn:
        assert (
            conn.execute(
                """
                SELECT COUNT(*)
                FROM artifact_locations
                WHERE location_id = ?
                """,
                (target["location_id"],),
            ).fetchone()[0]
            == 3
        )
        assert (
            conn.execute(
                """
                SELECT COUNT(*)
                FROM repository_stats_points
                WHERE location_id = ?
                """,
                (target["location_id"],),
            ).fetchone()[0]
            == 2
        )


@pytest.mark.parametrize("same_as_source", [False, True])
def test_merge_location_no_op_returns_false_and_leaves_locations_untouched(
    store: RepositoryArtifactStore, same_as_source: bool
) -> None:
    assert store.persist_refresh(
        job="job1",
        backup="main",
        backend="restic",
        backend_repository_id="repo-1",
        repository_location_key="local:/repo-a",
        display_repository="/repo-a",
        artifacts=[_snapshot("shared")],
        stats=_stats(total_size_bytes=1024),
        observed_at="2026-06-01T12:00:00+00:00",
    )
    source = store.resolve_location("local:/repo-a")
    assert source is not None
    target_location_id = str(source["location_id"]) if same_as_source else "missing-location-id"

    assert (
        store.merge_location(
            str(source["repository_id"]), str(source["location_id"]), target_location_id
        )
        is False
    )

    assert store.resolve_location("local:/repo-a") is not None
    artifacts = store.list_present_artifacts(
        str(source["repository_id"]), location_id=str(source["location_id"])
    )
    assert [artifact["backend_artifact_id"] for artifact in artifacts] == ["shared"]


def test_read_primitives_return_repository_artifacts_growth_and_detail(
    store: RepositoryArtifactStore,
) -> None:
    assert store.persist_backup_run(
        job="job1",
        backup="main",
        backend="restic",
        backend_repository_id="repo-1",
        repository_location_key="local:/repo",
        display_repository="/repo",
        artifacts=[_snapshot("snap-1")],
        context=BackupRunStatsContext(
            run_id="run-1",
            backup_summary=_snapshot("snap-1")["summary"],
            trigger_kind="backup",
        ),
        stats=_stats(total_size_bytes=4096),
    )
    assert store.persist_refresh(
        job="job1",
        backup="main",
        backend="restic",
        backend_repository_id="repo-1",
        repository_location_key="local:/repo",
        display_repository="/repo",
        artifacts=[_snapshot("snap-1"), _snapshot("snap-2")],
        stats=_stats(
            collected_at="2026-06-02T12:01:00+00:00",
            total_size_bytes=8192,
        ),
    )

    repo = store.resolve_location("local:/repo")
    assert repo is not None
    artifacts = store.list_present_artifacts(
        repository_id=repo["repository_id"],
        location_id=repo["location_id"],
    )
    growth = store.growth_points(repo["repository_id"])
    detail = artifacts[0]
    created_detail = artifacts[1]

    assert [artifact["backend_artifact_id"] for artifact in artifacts] == [
        "snap-2",
        "snap-1",
    ]
    assert [point["total_size_bytes"] for point in growth] == [4096, 8192]
    assert detail["metadata"]["raw_metadata"]["id"] == "snap-2"
    assert detail["summary"]["files_new"] == 1
    assert "raw_summary" not in detail["summary"]
    assert created_detail is not None
    assert created_detail["summary"]["raw_summary"]["snapshot_id"] == "snap-1"


def test_retention_primitives_cascade_locations_and_remove_only_unprotected_artifacts(
    store: RepositoryArtifactStore,
) -> None:
    assert store.persist_refresh(
        job="job1",
        backup="main",
        backend="restic",
        backend_repository_id="repo-1",
        repository_location_key="local:/repo",
        display_repository="/repo",
        artifacts=[
            _snapshot("present", created_at="2026-01-01T12:00:00+00:00"),
            _snapshot("removed", created_at="2026-01-01T12:00:00+00:00"),
        ],
        stats=_stats(),
    )
    assert store.persist_refresh(
        job="job1",
        backup="main",
        backend="restic",
        backend_repository_id="repo-1",
        repository_location_key="local:/repo",
        display_repository="/repo",
        artifacts=[_snapshot("present", created_at="2026-01-01T12:00:00+00:00")],
        stats=_stats(collected_at="2026-06-02T12:01:00+00:00"),
    )

    store.prune_artifacts(retention_days=1, retention_count=1000, now="2026-06-10T00:00:00+00:00")

    repo = store.resolve_location("local:/repo")
    assert repo is not None
    present = store.list_present_artifacts(
        repository_id=repo["repository_id"],
        location_id=repo["location_id"],
    )
    assert [artifact["backend_artifact_id"] for artifact in present] == ["present"]
    with closing(sqlite3.connect(store.db_path)) as conn:
        artifact_ids = {row[0] for row in conn.execute("SELECT backend_artifact_id FROM artifacts")}
        stats_run_step_ids = {
            row[0] for row in conn.execute("SELECT run_step_id FROM repository_stats_points")
        }
    assert artifact_ids == {"present"}
    assert None in stats_run_step_ids


def test_persist_refresh_does_not_delete_artifacts_without_retention_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DK_APPDATA_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("DK_APPDATA_RETENTION_COUNT", raising=False)
    store = RepositoryArtifactStore(db_path=tmp_path / "appdata.db")

    assert store.persist_refresh(
        job="job1",
        backup="main",
        backend="restic",
        backend_repository_id="repo-1",
        repository_location_key="local:/repo",
        display_repository="/repo",
        artifacts=[
            _snapshot("present", created_at="2026-01-01T12:00:00+00:00"),
            _snapshot("removed", created_at="2026-01-01T12:00:00+00:00"),
        ],
        stats=_stats(collected_at="2026-06-01T12:00:00+00:00"),
        observed_at="2026-06-01T12:00:00+00:00",
    )
    assert store.persist_refresh(
        job="job1",
        backup="main",
        backend="restic",
        backend_repository_id="repo-1",
        repository_location_key="local:/repo",
        display_repository="/repo",
        artifacts=[_snapshot("present", created_at="2026-01-01T12:00:00+00:00")],
        stats=_stats(collected_at="2026-06-10T00:00:00+00:00"),
        observed_at="2026-06-10T00:00:00+00:00",
    )

    with closing(sqlite3.connect(store.db_path)) as conn:
        artifact_ids = {row[0] for row in conn.execute("SELECT backend_artifact_id FROM artifacts")}

    assert artifact_ids == {"present", "removed"}


def test_persist_refresh_applies_artifact_retention_only_for_non_present_active_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DK_APPDATA_RETENTION_DAYS", "1")
    monkeypatch.setenv("DK_APPDATA_RETENTION_COUNT", "1000")
    store = RepositoryArtifactStore(db_path=tmp_path / "appdata.db")

    assert store.persist_refresh(
        job="job1",
        backup="main",
        backend="restic",
        backend_repository_id="repo-1",
        repository_location_key="local:/repo",
        display_repository="/repo",
        artifacts=[
            _snapshot("present", created_at="2026-01-01T12:00:00+00:00"),
            _snapshot("removed", created_at="2026-01-01T12:00:00+00:00"),
        ],
        stats=_stats(collected_at="2026-06-01T12:00:00+00:00"),
        observed_at="2026-06-01T12:00:00+00:00",
    )
    assert store.persist_refresh(
        job="job1",
        backup="main",
        backend="restic",
        backend_repository_id="repo-1",
        repository_location_key="local:/repo",
        display_repository="/repo",
        artifacts=[_snapshot("present", created_at="2026-01-01T12:00:00+00:00")],
        stats=_stats(collected_at="2026-06-10T00:00:00+00:00"),
        observed_at="2026-06-10T00:00:00+00:00",
    )

    with closing(sqlite3.connect(store.db_path)) as conn:
        artifact_ids = {row[0] for row in conn.execute("SELECT backend_artifact_id FROM artifacts")}

    assert artifact_ids == {"present"}


def test_persist_refresh_applies_artifact_retention_once_per_day(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DK_APPDATA_RETENTION_DAYS", "1")
    monkeypatch.setenv("DK_APPDATA_RETENTION_COUNT", "1000")
    store = RepositoryArtifactStore(db_path=tmp_path / "appdata.db")

    assert store.persist_refresh(
        job="job1",
        backup="main",
        backend="restic",
        backend_repository_id="repo-1",
        repository_location_key="local:/repo",
        display_repository="/repo",
        artifacts=[
            _snapshot("present", created_at="2026-01-01T12:00:00+00:00"),
            _snapshot("removed", created_at="2026-01-01T12:00:00+00:00"),
        ],
        stats=_stats(collected_at="2026-06-01T08:00:00+00:00"),
        observed_at="2026-06-01T08:00:00+00:00",
    )
    assert store.persist_refresh(
        job="job1",
        backup="main",
        backend="restic",
        backend_repository_id="repo-1",
        repository_location_key="local:/repo",
        display_repository="/repo",
        artifacts=[_snapshot("present", created_at="2026-01-01T12:00:00+00:00")],
        stats=_stats(collected_at="2026-06-01T12:00:00+00:00"),
        observed_at="2026-06-01T12:00:00+00:00",
    )

    with closing(sqlite3.connect(store.db_path)) as conn:
        same_day_artifacts = {
            row[0] for row in conn.execute("SELECT backend_artifact_id FROM artifacts")
        }
    assert same_day_artifacts == {"present", "removed"}

    assert store.persist_refresh(
        job="job1",
        backup="main",
        backend="restic",
        backend_repository_id="repo-1",
        repository_location_key="local:/repo",
        display_repository="/repo",
        artifacts=[_snapshot("present", created_at="2026-01-01T12:00:00+00:00")],
        stats=_stats(collected_at="2026-06-02T00:00:00+00:00"),
        observed_at="2026-06-02T00:00:00+00:00",
    )

    with closing(sqlite3.connect(store.db_path)) as conn:
        next_day_artifacts = {
            row[0] for row in conn.execute("SELECT backend_artifact_id FROM artifacts")
        }
    assert next_day_artifacts == {"present"}


def test_delete_location_removes_only_artifacts_without_other_active_locations(
    store: RepositoryArtifactStore,
) -> None:
    assert store.persist_refresh(
        job="job1",
        backup="main",
        backend="restic",
        backend_repository_id="repo-1",
        repository_location_key="local:/source",
        display_repository="/source",
        artifacts=[
            _snapshot("shared", created_at="2026-01-01T12:00:00+00:00"),
            _snapshot("source-only", created_at="2026-01-01T13:00:00+00:00"),
        ],
        stats=_stats(collected_at="2026-06-01T12:00:00+00:00"),
        observed_at="2026-06-01T12:00:00+00:00",
    )
    assert store.persist_refresh(
        job="job1",
        backup="main",
        backend="restic",
        backend_repository_id="repo-1",
        repository_location_key="local:/target",
        display_repository="/target",
        artifacts=[_snapshot("shared", created_at="2026-01-01T12:00:00+00:00")],
        stats=_stats(collected_at="2026-06-02T12:00:00+00:00"),
        observed_at="2026-06-02T12:00:00+00:00",
    )

    with closing(store.connect()) as conn:
        source = conn.execute("""
            SELECT repository_id, location_id
            FROM repository_locations
            WHERE display_repository = '/source'
            """).fetchone()
        target = conn.execute("""
            SELECT location_id
            FROM repository_locations
            WHERE display_repository = '/target'
            """).fetchone()
    assert source is not None
    assert target is not None
    assert store.delete_location(str(source["repository_id"]), str(source["location_id"]))

    with closing(store.connect()) as conn:
        artifacts = conn.execute(
            "SELECT backend_artifact_id FROM artifacts ORDER BY backend_artifact_id"
        ).fetchall()
        stats_locations = conn.execute(
            "SELECT DISTINCT location_id FROM repository_stats_points"
        ).fetchall()

    assert [row["backend_artifact_id"] for row in artifacts] == ["shared"]
    assert {row["location_id"] for row in stats_locations} == {str(target["location_id"])}


def test_find_or_create_repository_resolves_one_row_across_two_connections(
    store: RepositoryArtifactStore,
) -> None:
    with closing(store.connect()) as conn_a, closing(store.connect()) as conn_b:
        conn_a.execute("BEGIN IMMEDIATE")
        uuid_a = find_or_create_repository(
            conn_a,
            backend="restic",
            backend_repository_id="repo-shared",
            now="2026-01-01T00:00:00+00:00",
        )
        conn_a.execute("COMMIT")

        conn_b.execute("BEGIN IMMEDIATE")
        uuid_b = find_or_create_repository(
            conn_b,
            backend="restic",
            backend_repository_id="repo-shared",
            now="2026-01-02T00:00:00+00:00",
        )
        conn_b.execute("COMMIT")

    assert uuid_a == uuid_b
    with closing(sqlite3.connect(store.db_path)) as conn:
        rows = conn.execute(
            "SELECT repository_id FROM repositories WHERE backend_repository_id = ?",
            ("repo-shared",),
        ).fetchall()
    assert [row[0] for row in rows] == [uuid_a]


def test_concurrent_first_observation_of_same_repository_id_stays_unique(
    store: RepositoryArtifactStore,
) -> None:
    """Concurrent threads racing to observe an unknown repository ID must
    converge on exactly one ``repositories`` row, guarded by the
    ``UNIQUE(backend, backend_repository_id)`` constraint."""
    barrier = threading.Barrier(8)

    def observe(index: int) -> str:
        barrier.wait()
        return store.find_or_create_repository(
            backend="restic",
            backend_repository_id="shared-repo-id",
            first_seen_at=f"2026-01-01T00:00:{index:02d}+00:00",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(observe, index) for index in range(8)]
        repository_ids = {future.result() for future in futures}

    assert len(repository_ids) == 1
    with closing(sqlite3.connect(store.db_path)) as conn:
        rows = conn.execute(
            "SELECT repository_id FROM repositories WHERE backend_repository_id = ?",
            ("shared-repo-id",),
        ).fetchall()
    assert [row[0] for row in rows] == [repository_ids.pop()]


def test_concurrent_resolve_observed_location_for_same_repository_id_stays_unique(
    store: RepositoryArtifactStore,
) -> None:
    """Same race as above through ``resolve_observed_location``, which is the
    path the stats collector actually uses for first-time repository sightings."""
    barrier = threading.Barrier(8)

    def observe(index: int) -> dict[str, object]:
        barrier.wait()
        return store.resolve_observed_location(
            backend="restic",
            backend_repository_id="shared-repo-id-2",
            repository_location_key=repository_location_key(f"/repo-shared-{index % 2}"),
            display_repository=f"/repo-shared-{index % 2}",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(observe, index) for index in range(8)]
        repository_ids = {str(future.result()["repository_id"]) for future in futures}

    assert len(repository_ids) == 1
    with closing(sqlite3.connect(store.db_path)) as conn:
        rows = conn.execute(
            "SELECT repository_id FROM repositories WHERE backend_repository_id = ?",
            ("shared-repo-id-2",),
        ).fetchall()
    assert [row[0] for row in rows] == [repository_ids.pop()]


def test_persist_backup_run_with_retention_trigger_skips_stats_point_but_reconciles_artifacts(
    store: RepositoryArtifactStore,
) -> None:
    """Retention post-processing must not crash on the ``trigger`` CHECK
    constraint (only 'backup', 'cleanup', 'refresh' are allowed) and must still
    reconcile artifact presence via the full-reconcile path."""
    assert store.persist_backup_run(
        job="job1",
        backup="main",
        backend="restic",
        backend_repository_id="repo-1",
        repository_location_key="local:/repo",
        display_repository="/repo",
        artifacts=[_snapshot("kept")],
        stats=_stats(),
        context=BackupRunStatsContext(run_step_id="step-1", trigger_kind="backup"),
    )

    result = store.persist_backup_run(
        job="job1",
        backup="main",
        backend="restic",
        backend_repository_id="repo-1",
        repository_location_key="local:/repo",
        display_repository="/repo",
        artifacts=[_snapshot("kept")],
        full_reconcile=True,
        context=BackupRunStatsContext(run_step_id="step-2", trigger_kind="retention"),
        observed_at="2026-06-02T00:00:00+00:00",
    )

    assert result is True
    repo = store.resolve_location("local:/repo")
    assert repo is not None
    present = store.list_present_artifacts(
        repository_id=repo["repository_id"],
        location_id=repo["location_id"],
    )
    assert [artifact["backend_artifact_id"] for artifact in present] == ["kept"]
    with closing(sqlite3.connect(store.db_path)) as conn:
        trigger_values = {
            row[0] for row in conn.execute("SELECT trigger FROM repository_stats_points")
        }
    assert trigger_values == {"backup"}


def test_full_reconcile_records_removing_run_step_on_artifact_locations(
    store: RepositoryArtifactStore,
) -> None:
    """A full-reconcile removal must record which run_step detected it, so the
    observation stays attributable even though the run/run_step may later be
    deleted by run-history retention (``removed_by_run_step_id ON DELETE SET NULL``)."""
    assert store.persist_backup_run(
        job="job1",
        backup="main",
        backend="restic",
        backend_repository_id="repo-1",
        repository_location_key="local:/repo",
        display_repository="/repo",
        artifacts=[_snapshot("kept"), _snapshot("gone")],
        stats=_stats(),
        context=BackupRunStatsContext(run_step_id="step-1", trigger_kind="backup"),
    )

    with closing(store.connect()) as conn:
        conn.execute("""
            INSERT INTO runs (
                run_id, origin, run_kind, job, task_type, task_name,
                started_at, status, dry_run
            )
            VALUES ('run-2', 'manual', 'job_task', 'job1', 'backup', 'main',
                    '2026-06-02T00:00:00+00:00', 'running', 0)
            """)
        conn.execute("""
            INSERT INTO run_steps (
                run_step_id, run_id, position, step, backend, task_type, task_name,
                started_at, status, effective_task_config_json
            )
            VALUES ('step-retention', 'run-2', 1, 'backup.main.retention', 'restic',
                    'backup', 'main', '2026-06-02T00:00:00+00:00', 'running', '{}')
            """)
        conn.commit()

    assert store.persist_backup_run(
        job="job1",
        backup="main",
        backend="restic",
        backend_repository_id="repo-1",
        repository_location_key="local:/repo",
        display_repository="/repo",
        artifacts=[_snapshot("kept")],
        full_reconcile=True,
        context=BackupRunStatsContext(
            run_id="run-2",
            run_step_id="step-retention",
            trigger_kind="retention",
        ),
        observed_at="2026-06-02T00:00:01+00:00",
    )

    with closing(store.connect()) as conn:
        kept_row = conn.execute("""
            SELECT artifact_locations.present, artifact_locations.removed_by_run_step_id
            FROM artifact_locations
            JOIN artifacts ON artifacts.artifact_id = artifact_locations.artifact_id
            WHERE artifacts.backend_artifact_id = 'kept'
            """).fetchone()
        gone_row = conn.execute("""
            SELECT artifact_locations.present, artifact_locations.removed_by_run_step_id
            FROM artifact_locations
            JOIN artifacts ON artifacts.artifact_id = artifact_locations.artifact_id
            WHERE artifacts.backend_artifact_id = 'gone'
            """).fetchone()

    assert kept_row["present"] == 1
    assert kept_row["removed_by_run_step_id"] is None
    assert gone_row["present"] == 0
    assert gone_row["removed_by_run_step_id"] == "step-retention"


def test_retention_reactivates_previously_removed_location_observation(
    store: RepositoryArtifactStore,
) -> None:
    """A snapshot the backend stops listing is marked removed by retention's
    full-reconcile; if a later retention run lists it again (e.g. transient
    backend listing gap), the location observation must flip back to present
    and clear the prior removal markers rather than staying removed forever."""
    assert store.persist_backup_run(
        job="job1",
        backup="main",
        backend="restic",
        backend_repository_id="repo-1",
        repository_location_key="local:/repo",
        display_repository="/repo",
        artifacts=[_snapshot("kept"), _snapshot("flaky")],
        stats=_stats(),
        context=BackupRunStatsContext(run_step_id="step-1", trigger_kind="backup"),
    )

    with closing(store.connect()) as conn:
        conn.execute("""
            INSERT INTO runs (
                run_id, origin, run_kind, job, task_type, task_name,
                started_at, status, dry_run
            )
            VALUES ('run-2', 'manual', 'job_task', 'job1', 'backup', 'main',
                    '2026-06-02T00:00:00+00:00', 'running', 0)
            """)
        conn.execute("""
            INSERT INTO run_steps (
                run_step_id, run_id, position, step, backend, task_type, task_name,
                started_at, status, effective_task_config_json
            )
            VALUES ('step-retention-1', 'run-2', 1, 'backup.main.retention', 'restic',
                    'backup', 'main', '2026-06-02T00:00:00+00:00', 'running', '{}')
            """)
        conn.commit()

    assert store.persist_backup_run(
        job="job1",
        backup="main",
        backend="restic",
        backend_repository_id="repo-1",
        repository_location_key="local:/repo",
        display_repository="/repo",
        artifacts=[_snapshot("kept")],
        full_reconcile=True,
        context=BackupRunStatsContext(
            run_id="run-2",
            run_step_id="step-retention-1",
            trigger_kind="retention",
        ),
        observed_at="2026-06-02T00:00:01+00:00",
    )

    with closing(store.connect()) as conn:
        removed_row = conn.execute("""
            SELECT artifact_locations.present, artifact_locations.removed_at,
                   artifact_locations.removed_by_run_step_id
            FROM artifact_locations
            JOIN artifacts ON artifacts.artifact_id = artifact_locations.artifact_id
            WHERE artifacts.backend_artifact_id = 'flaky'
            """).fetchone()
    assert removed_row["present"] == 0
    assert removed_row["removed_at"] is not None
    assert removed_row["removed_by_run_step_id"] == "step-retention-1"

    with closing(store.connect()) as conn:
        conn.execute("""
            INSERT INTO runs (
                run_id, origin, run_kind, job, task_type, task_name,
                started_at, status, dry_run
            )
            VALUES ('run-3', 'manual', 'job_task', 'job1', 'backup', 'main',
                    '2026-06-03T00:00:00+00:00', 'running', 0)
            """)
        conn.execute("""
            INSERT INTO run_steps (
                run_step_id, run_id, position, step, backend, task_type, task_name,
                started_at, status, effective_task_config_json
            )
            VALUES ('step-retention-2', 'run-3', 1, 'backup.main.retention', 'restic',
                    'backup', 'main', '2026-06-03T00:00:00+00:00', 'running', '{}')
            """)
        conn.commit()

    assert store.persist_backup_run(
        job="job1",
        backup="main",
        backend="restic",
        backend_repository_id="repo-1",
        repository_location_key="local:/repo",
        display_repository="/repo",
        artifacts=[_snapshot("kept"), _snapshot("flaky")],
        full_reconcile=True,
        context=BackupRunStatsContext(
            run_id="run-3",
            run_step_id="step-retention-2",
            trigger_kind="retention",
        ),
        observed_at="2026-06-03T00:00:01+00:00",
    )

    with closing(store.connect()) as conn:
        reactivated_row = conn.execute("""
            SELECT artifact_locations.present, artifact_locations.removed_at,
                   artifact_locations.removed_by_run_step_id
            FROM artifact_locations
            JOIN artifacts ON artifacts.artifact_id = artifact_locations.artifact_id
            WHERE artifacts.backend_artifact_id = 'flaky'
            """).fetchone()
    assert reactivated_row["present"] == 1
    assert reactivated_row["removed_at"] is None
    assert reactivated_row["removed_by_run_step_id"] is None
