import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

from src.services.appdata_schema import (
    SCHEMA_GENERATION,
    TARGET_TABLES,
    connect_appdata_db,
    immediate_tx,
    utc_rfc3339,
)


class _StopTransaction(BaseException):
    pass


def test_utc_rfc3339_normalizes_to_utc_with_literal_z_suffix() -> None:
    naive_like_utc = datetime(2026, 6, 28, 9, 0, 0, tzinfo=timezone.utc)
    assert utc_rfc3339(naive_like_utc) == "2026-06-28T09:00:00.000000Z"

    non_utc = datetime(2026, 6, 28, 11, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    assert utc_rfc3339(non_utc) == "2026-06-28T09:00:00.000000Z"


def test_missing_generation_resets_and_writes_target_generation(tmp_path):
    db_path = tmp_path / "appdata.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, task_selector TEXT)")
        conn.execute("INSERT INTO runs VALUES ('old-run', 'demo.backup.local')")
        conn.commit()

    with closing(connect_appdata_db(db_path)) as conn:
        generation = conn.execute(
            "SELECT value FROM appdata_meta WHERE key = 'schema_generation'"
        ).fetchone()[0]
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
        rows = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

    assert generation == SCHEMA_GENERATION
    assert "task_selector" not in columns
    assert rows == 0


def test_wrong_generation_drops_old_and_target_tables(tmp_path):
    db_path = tmp_path / "appdata.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE appdata_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO appdata_meta VALUES ('schema_generation', 'old')")
        conn.execute("CREATE TABLE backup_artifacts (artifact_id TEXT)")
        conn.execute("CREATE TABLE backup_custom_cache (id TEXT)")
        conn.execute("CREATE TABLE restores (restore_id TEXT)")
        conn.execute("CREATE TABLE unrelated_local_table (id TEXT)")
        conn.commit()

    with closing(connect_appdata_db(db_path)) as conn:
        tables = _tables(conn)

    assert "backup_artifacts" not in tables
    assert "backup_custom_cache" not in tables
    assert "restores" not in tables
    assert "unrelated_local_table" in tables
    assert set(TARGET_TABLES).issubset(tables)


def test_current_generation_with_wrong_shape_is_rebuilt(tmp_path):
    db_path = tmp_path / "appdata.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE appdata_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO appdata_meta VALUES ('schema_generation', ?)",
            (SCHEMA_GENERATION,),
        )
        conn.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, task_selector TEXT)")
        conn.commit()

    with closing(connect_appdata_db(db_path)) as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}

    assert "task_selector" not in columns
    assert "run_kind" in columns


def test_target_schema_and_indexes_exist(tmp_path):
    with closing(connect_appdata_db(tmp_path / "appdata.db")) as conn:
        tables = _tables(conn)
        indexes = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        foreign_keys = {
            table: conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
            for table in TARGET_TABLES
        }

    assert set(TARGET_TABLES).issubset(tables)
    assert {
        "idx_run_steps_run_id",
        "idx_run_restores_run_id",
        "idx_artifact_locations_location_id",
        "idx_repository_stats_points_location_id",
        "idx_repository_stats_points_collected_at",
    }.issubset(indexes)
    assert foreign_keys["run_steps"][0]["on_delete"].upper() == "CASCADE"
    assert any(
        row["table"] == "run_steps" and row["on_delete"].upper() == "SET NULL"
        for row in foreign_keys["artifacts"]
    )


def test_immediate_tx_rolls_back_partial_write_on_exception(tmp_path):
    db_path = tmp_path / "appdata.db"

    with pytest.raises(_StopTransaction):
        with immediate_tx(lambda: connect_appdata_db(db_path)) as conn:
            _insert_repository(conn, "repo-1")
            raise _StopTransaction()

    with closing(connect_appdata_db(db_path)) as conn:
        rows = conn.execute("SELECT backend_repository_id FROM repositories").fetchall()

    assert rows == []


def test_unique_constraints_are_enforced(tmp_path):
    with closing(connect_appdata_db(tmp_path / "appdata.db")) as conn:
        _insert_repository(conn, "repo-1")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_repository(conn, "repo-2")

        _insert_location(conn, "loc-1", "repo-1")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_location(conn, "loc-1-again", "repo-1", location_key="loc-1")

        _insert_run(conn, "run-1")
        _insert_step(conn, "step-1", "run-1", position=1)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_step(conn, "step-2", "run-1", position=1)

        _insert_run(conn, "restore-run", run_kind="restore", task_type="restore")
        _insert_restore(conn, "restore-1", "restore-run")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_restore(conn, "restore-2", "restore-run")

        _insert_artifact(conn, "artifact-1", "repo-1", backend_artifact_id="snapshot-1")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_artifact(conn, "artifact-2", "repo-1", backend_artifact_id="snapshot-1")

        _insert_artifact_location(conn, "artifact-loc-1", "artifact-1", "loc-1")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_artifact_location(conn, "artifact-loc-2", "artifact-1", "loc-1")


def test_check_constraints_are_enforced(tmp_path):
    with closing(connect_appdata_db(tmp_path / "appdata.db")) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_run(conn, "run-1", status="bad")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_run(conn, "run-2", run_kind="bad")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_run(conn, "run-3", dry_run=2)

        _insert_run(conn, "run-4")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_step(conn, "step-1", "run-4", status="bad")

        _insert_repository(conn, "repo-1")
        _insert_location(conn, "loc-1", "repo-1")
        _insert_stats_point(conn, "stats-cleanup", "repo-1", "loc-1", trigger="cleanup")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_stats_point(conn, "stats-1", "repo-1", "loc-1", trigger="bad")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_stats_point(conn, "stats-2", "repo-1", "loc-1", total_size=-1)

        with pytest.raises(sqlite3.IntegrityError):
            _insert_artifact(conn, "artifact-1", "repo-1", files_new=-1)
        _insert_artifact(conn, "artifact-2", "repo-1")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_artifact_location(conn, "artifact-loc-1", "artifact-2", "loc-1", present=2)


def test_foreign_key_delete_behaviors(tmp_path):
    with closing(connect_appdata_db(tmp_path / "appdata.db")) as conn:
        _insert_run(conn, "run-1")
        _insert_step(conn, "step-1", "run-1")
        _insert_run(conn, "restore-run", run_kind="restore", task_type="restore")
        _insert_restore(conn, "restore-1", "restore-run")
        _insert_repository(conn, "repo-1")
        _insert_location(conn, "loc-1", "repo-1")
        _insert_artifact(conn, "artifact-1", "repo-1", created_run_step_id="step-1")
        _insert_artifact_location(
            conn,
            "artifact-loc-1",
            "artifact-1",
            "loc-1",
            removed_by_run_step_id="step-1",
        )
        _insert_stats_point(
            conn,
            "stats-1",
            "repo-1",
            "loc-1",
            run_step_id="step-1",
        )

        conn.execute("DELETE FROM runs WHERE run_id = 'run-1'")

        assert conn.execute(
            "SELECT run_id FROM run_restores WHERE run_id = 'restore-run'"
        ).fetchone()
        conn.execute("DELETE FROM runs WHERE run_id = 'restore-run'")
        assert conn.execute("SELECT COUNT(*) FROM run_restores").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT run_step_id FROM repository_stats_points WHERE stats_point_id = 'stats-1'"
            ).fetchone()[0]
            is None
        )
        assert (
            conn.execute("SELECT removed_by_run_step_id FROM artifact_locations").fetchone()[0]
            is None
        )
        assert (
            conn.execute(
                "SELECT created_run_step_id FROM artifacts WHERE artifact_id = 'artifact-1'"
            ).fetchone()[0]
            is None
        )

        conn.execute("DELETE FROM repository_locations WHERE location_id = 'loc-1'")
        assert conn.execute("SELECT COUNT(*) FROM artifact_locations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM repository_stats_points").fetchone()[0] == 0


def test_connection_helper_enables_foreign_keys(tmp_path):
    with closing(connect_appdata_db(tmp_path / "appdata.db")) as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            _insert_step(conn, "orphan-step", "missing-run")


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }


def _insert_repository(conn: sqlite3.Connection, repository_id: str) -> None:
    conn.execute(
        """
        INSERT INTO repositories (repository_id, backend, backend_repository_id)
        VALUES (?, 'restic', 'backend-repo')
        """,
        (repository_id,),
    )


def _insert_location(
    conn: sqlite3.Connection,
    location_id: str,
    repository_id: str,
    *,
    location_key: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO repository_locations (
            location_id, repository_id, repository_location_key, display_repository
        )
        VALUES (?, ?, ?, '/backups/demo')
        """,
        (location_id, repository_id, location_key or location_id),
    )


def _insert_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    run_kind: str = "job_task",
    task_type: str = "backup",
    status: str = "running",
    dry_run: int = 0,
) -> None:
    conn.execute(
        """
        INSERT INTO runs (
            run_id, origin, run_kind, job, task_type, task_name, started_at, status, dry_run
        )
        VALUES (?, 'manual', ?, 'demo', ?, 'local', '2026-06-28T09:00:00Z', ?, ?)
        """,
        (run_id, run_kind, task_type, status, dry_run),
    )


def _insert_step(
    conn: sqlite3.Connection,
    run_step_id: str,
    run_id: str,
    *,
    position: int = 1,
    status: str = "running",
) -> None:
    conn.execute(
        """
        INSERT INTO run_steps (
            run_step_id, run_id, position, step, backend, task_type, task_name,
            started_at, status, effective_task_config_json
        )
        VALUES (?, ?, ?, 'backup.local.backup', 'restic', 'backup', 'local',
                '2026-06-28T09:00:00Z', ?, '{}')
        """,
        (run_step_id, run_id, position, status),
    )


def _insert_restore(
    conn: sqlite3.Connection,
    restore_id: str,
    run_id: str,
) -> None:
    conn.execute(
        """
        INSERT INTO run_restores (
            run_restore_id, run_id, job, backup, backend, snapshot_id, mode,
            restore_target, snapshot_paths_json, include_patterns_json,
            exclude_patterns_json, overwrite, output_truncated
        )
        VALUES (?, ?, 'demo', 'local', 'restic', 'snapshot-1', 'browser',
                '/restore/demo', '[]', '[]', '[]', 0, 0)
        """,
        (restore_id, run_id),
    )


def _insert_artifact(
    conn: sqlite3.Connection,
    artifact_id: str,
    repository_id: str,
    *,
    backend_artifact_id: str | None = None,
    created_run_step_id: str | None = None,
    files_new: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO artifacts (
            artifact_id, repository_id, backend, backend_artifact_id, created_at,
            created_run_step_id, files_new, paths_json, tags_json, raw_snapshot_json
        )
        VALUES (?, ?, 'restic', ?, '2026-06-28T09:00:00Z', ?, ?, '[]', '[]', '{}')
        """,
        (
            artifact_id,
            repository_id,
            backend_artifact_id or artifact_id,
            created_run_step_id,
            files_new,
        ),
    )


def _insert_artifact_location(
    conn: sqlite3.Connection,
    artifact_location_id: str,
    artifact_id: str,
    location_id: str,
    *,
    present: int = 1,
    removed_by_run_step_id: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO artifact_locations (
            artifact_location_id, artifact_id, location_id, present,
            removed_by_run_step_id
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (artifact_location_id, artifact_id, location_id, present, removed_by_run_step_id),
    )


def _insert_stats_point(
    conn: sqlite3.Connection,
    stats_point_id: str,
    repository_id: str,
    location_id: str,
    *,
    run_step_id: str | None = None,
    trigger: str = "backup",
    total_size: int | None = 10,
) -> None:
    conn.execute(
        """
        INSERT INTO repository_stats_points (
            stats_point_id, repository_id, location_id, run_step_id, trigger,
            collected_at, total_size_bytes, artifacts_count, raw_stats_json
        )
        VALUES (?, ?, ?, ?, ?, '2026-06-28T09:00:00Z', ?, 1, '{}')
        """,
        (stats_point_id, repository_id, location_id, run_step_id, trigger, total_size),
    )
