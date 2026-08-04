import asyncio
import concurrent.futures
import json
from collections.abc import Callable, Coroutine
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

import pytest

from src.services.run_history import (
    DEFAULT_APPDATA_RETENTION_COUNT,
    DEFAULT_APPDATA_RETENTION_DAYS,
    RunHistoryService,
    appdata_retention_count,
    appdata_retention_days,
    default_appdata_db_path,
)
from src.services.run_manager import RunKind, RunOrigin, RunRecord, RunStatus

P = ParamSpec("P")
T = TypeVar("T")


@pytest.fixture(autouse=True)
def run_to_thread_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def immediate(func: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", immediate)


def _run_async(coro: Coroutine[Any, Any, None]) -> None:
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    finally:
        loop.close()


def _record(
    run_id: str,
    *,
    status: RunStatus = RunStatus.SUCCESS,
    origin: RunOrigin = RunOrigin.MANUAL,
    finished_at: datetime | None = None,
    job: str = "demo",
    task_type: str = "backup",
    task_name: str = "local",
    run_kind: RunKind = RunKind.JOB_TASK,
    error: str | None = None,
) -> RunRecord:
    created_at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    return RunRecord(
        run_id=run_id,
        origin=origin,
        run_kind=run_kind,
        job=job,
        task_type=task_type,
        task_name=task_name,
        status=status,
        dry_run=True,
        cancellable=False,
        created_at=created_at,
        started_at=created_at + timedelta(minutes=1),
        finished_at=finished_at or created_at + timedelta(minutes=2),
        error=error,
    )


def test_default_appdata_db_path_uses_shared_appdata_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DK_APPDATA_DIR", "/custom/appdata")

    assert default_appdata_db_path() == Path("/custom/appdata/appdata.db")


def test_default_appdata_db_path_uses_appdata_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DK_APPDATA_DIR", raising=False)

    assert default_appdata_db_path() == Path("/appdata/appdata.db")


def test_appdata_retention_is_disabled_by_default_and_for_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DK_APPDATA_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("DK_APPDATA_RETENTION_COUNT", raising=False)

    assert DEFAULT_APPDATA_RETENTION_DAYS is None
    assert DEFAULT_APPDATA_RETENTION_COUNT is None
    assert appdata_retention_days() is None
    assert appdata_retention_count() is None

    monkeypatch.setenv("DK_APPDATA_RETENTION_DAYS", "abc")
    monkeypatch.setenv("DK_APPDATA_RETENTION_COUNT", "0")

    assert appdata_retention_days() is None
    assert appdata_retention_count() is None

    monkeypatch.setenv("DK_APPDATA_RETENTION_DAYS", "")
    monkeypatch.setenv("DK_APPDATA_RETENTION_COUNT", "")

    assert appdata_retention_days() is None
    assert appdata_retention_count() is None


def test_record_does_not_prune_without_appdata_retention_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DK_APPDATA_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("DK_APPDATA_RETENTION_COUNT", raising=False)

    async def scenario() -> None:
        service = RunHistoryService(tmp_path / "appdata.db")
        base = datetime.now(timezone.utc)
        await service.record(_record("old", finished_at=base - timedelta(days=5000)))
        await service.record(_record("newer", finished_at=base + timedelta(minutes=1)))
        await service.record(_record("newest", finished_at=base + timedelta(minutes=2)))

        assert [record.run_id for record in await service.list_history()] == [
            "newest",
            "newer",
            "old",
        ]

    _run_async(scenario())


def test_record_get_and_list_history_roundtrip(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = RunHistoryService(tmp_path / "appdata.db")
        record = _record("run-1", status=RunStatus.FAILED, error="boom")

        await service.record(record)

        loaded = await service.get("run-1")
        assert loaded is not None
        assert loaded.run_id == "run-1"
        assert loaded.origin == RunOrigin.MANUAL
        assert loaded.run_kind == RunKind.JOB_TASK
        assert loaded.job == "demo"
        assert loaded.task_type == "backup"
        assert loaded.task_name == "local"
        assert loaded.status == RunStatus.FAILED
        assert loaded.dry_run is True
        assert loaded.cancellable is False
        assert loaded.created_at == record.started_at
        assert loaded.started_at == record.started_at
        assert loaded.finished_at == record.finished_at
        assert loaded.error == "boom"

        assert [entry.run_id for entry in await service.list_history()] == ["run-1"]

    _run_async(scenario())


def test_create_step_increments_position_and_finish_step_updates_terminal_fields(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "appdata.db"
        service = RunHistoryService(db_path)
        await service.create_run(_record("run-1", status=RunStatus.RUNNING))
        effective_config = {"repository": "/backups/local", "retention": {"keep_last": 3}}

        first_step_id = await service.create_step(
            run_id="run-1",
            step="backup.local.backup",
            backend="restic",
            task_type="backup",
            task_name="local",
            effective_task_config=effective_config,
        )
        second_step_id = await service.create_step(
            run_id="run-1",
            step="backup.local.retention",
            backend="restic",
            task_type="backup",
            task_name="local",
            effective_task_config={"retention": {"keep_last": 3}},
        )
        await service.finish_step(first_step_id, status=RunStatus.SUCCESS)
        await service.finish_step(second_step_id, status=RunStatus.FAILED, error="forget failed")

        with closing(service._connect()) as conn:
            rows = conn.execute("""
                SELECT run_step_id, position, step, status, error, finished_at,
                       effective_task_config_json
                FROM run_steps
                WHERE run_id = 'run-1'
                ORDER BY position
                """).fetchall()

        assert [row["run_step_id"] for row in rows] == [first_step_id, second_step_id]
        assert [row["position"] for row in rows] == [0, 1]
        assert [row["step"] for row in rows] == ["backup.local.backup", "backup.local.retention"]
        assert rows[0]["status"] == RunStatus.SUCCESS.value
        assert rows[0]["error"] is None
        assert rows[0]["finished_at"] is not None
        assert json.loads(rows[0]["effective_task_config_json"]) == effective_config
        assert rows[1]["status"] == RunStatus.FAILED.value
        assert rows[1]["error"] == "forget failed"

    _run_async(scenario())


def test_create_step_redacts_persisted_credentials(tmp_path: Path) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "appdata.db"
        service = RunHistoryService(db_path)
        await service.create_run(_record("run-1", status=RunStatus.RUNNING))

        direct_step_id = await service.create_step(
            run_id="run-1",
            step="backup.local.backup",
            backend="restic",
            task_type="backup",
            task_name="local",
            effective_task_config={
                "repository": "/repo",
                "credentials": {
                    "password": "direct-secret",
                    "password_env": None,
                    "password_file": None,
                },
            },
        )
        env_step_id = await service.create_step(
            run_id="run-1",
            step="backup.env.backup",
            backend="restic",
            task_type="backup",
            task_name="env",
            effective_task_config={
                "repository": "/repo",
                "credentials": {
                    "password": "resolved-secret",
                    "password_env": "RESTIC_PASSWORD",
                    "password_file": None,
                },
            },
        )
        file_step_id = await service.create_step(
            run_id="run-1",
            step="backup.file.backup",
            backend="restic",
            task_type="backup",
            task_name="file",
            effective_task_config={
                "repository": "/repo",
                "credentials": {
                    "password": None,
                    "password_env": None,
                    "password_file": "/run/secrets/restic-password",
                },
            },
        )

        with closing(service._connect()) as conn:
            rows = conn.execute("""
                SELECT run_step_id, effective_task_config_json
                FROM run_steps
                WHERE run_id = 'run-1'
                ORDER BY position
                """).fetchall()

        configs = {
            row["run_step_id"]: json.loads(row["effective_task_config_json"]) for row in rows
        }
        assert configs[direct_step_id]["credentials"] == {
            "password": "******",
            "password_env": None,
            "password_file": None,
        }
        assert configs[env_step_id]["credentials"] == {
            "password": None,
            "password_env": "RESTIC_PASSWORD",
            "password_file": None,
        }
        assert configs[file_step_id]["credentials"] == {
            "password": None,
            "password_env": None,
            "password_file": "/run/secrets/restic-password",
        }
        serialized = json.dumps(configs)
        assert "direct-secret" not in serialized
        assert "resolved-secret" not in serialized

    _run_async(scenario())


def test_mark_active_runs_interrupted_terminalizes_only_selected_origins(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        service = RunHistoryService(tmp_path / "appdata.db")
        await service.create_run(
            _record("manual", status=RunStatus.RUNNING, origin=RunOrigin.MANUAL)
        )
        await service.create_run(
            _record(
                "scheduler",
                status=RunStatus.RUNNING,
                origin=RunOrigin.SCHEDULER,
            )
        )

        changed = service.mark_active_runs_interrupted_sync(origins={RunOrigin.MANUAL})

        manual = await service.get("manual")
        scheduler = await service.get("scheduler")
        assert changed == 1
        assert manual is not None
        assert manual.status == RunStatus.UNEXPECTED_ERROR
        assert manual.finished_at is not None
        assert manual.error == "Run was interrupted by process restart"
        assert scheduler is not None
        assert scheduler.status == RunStatus.RUNNING
        assert scheduler.finished_at is None

    _run_async(scenario())


def test_list_history_sorts_newest_first_and_applies_limit(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = RunHistoryService(tmp_path / "appdata.db")
        base = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        await service.record(_record("old", finished_at=base))
        await service.record(_record("newer-b", finished_at=base + timedelta(minutes=5)))
        await service.record(_record("newer-a", finished_at=base + timedelta(minutes=5)))

        records = await service.list_history(limit=2)

        assert [record.run_id for record in records] == ["newer-b", "newer-a"]

    _run_async(scenario())


def test_count_failures_since_counts_only_failures_in_window(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = RunHistoryService(tmp_path / "appdata.db")
        base = datetime(2026, 1, 8, 12, 0, tzinfo=timezone.utc)
        await service.record(_record("success", status=RunStatus.SUCCESS, finished_at=base))
        await service.record(
            _record(
                "failed-outside",
                status=RunStatus.FAILED,
                finished_at=base - timedelta(days=8),
            )
        )
        await service.record(_record("failed-inside", status=RunStatus.FAILED, finished_at=base))
        await service.record(
            _record(
                "unexpected-inside",
                status=RunStatus.UNEXPECTED_ERROR,
                finished_at=base - timedelta(days=1),
            )
        )

        count = await service.count_failures_since(since=base - timedelta(days=7))

        assert count == 2

    _run_async(scenario())


def test_list_history_applies_offset(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = RunHistoryService(tmp_path / "appdata.db")
        base = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        await service.record(_record("oldest", finished_at=base))
        await service.record(_record("middle", finished_at=base + timedelta(minutes=1)))
        await service.record(_record("newest", finished_at=base + timedelta(minutes=2)))

        records = await service.list_history(limit=1, offset=1)

        assert [record.run_id for record in records] == ["middle"]

    _run_async(scenario())


def test_list_history_excludes_run_ids_before_limit_and_offset(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = RunHistoryService(tmp_path / "appdata.db")
        base = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        await service.record(_record("oldest", finished_at=base))
        await service.record(_record("middle", finished_at=base + timedelta(minutes=1)))
        await service.record(_record("live", finished_at=base + timedelta(minutes=2)))
        await service.record(_record("newest", finished_at=base + timedelta(minutes=3)))

        first_page = await service.list_history(limit=2, exclude_run_ids={"live"})
        second_page = await service.list_history(
            limit=2,
            offset=2,
            exclude_run_ids={"live"},
        )

        assert [record.run_id for record in first_page] == ["newest", "middle"]
        assert [record.run_id for record in second_page] == ["oldest"]

    _run_async(scenario())


def test_list_history_filters_by_job_task_status_and_origin_before_limit(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        service = RunHistoryService(tmp_path / "appdata.db")
        base = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        await service.record(
            _record(
                "wrong-origin-newest",
                job="demo",
                task_type="backup",
                task_name="local",
                status=RunStatus.FAILED,
                origin=RunOrigin.SCHEDULER,
                finished_at=base + timedelta(minutes=4),
            )
        )
        await service.record(
            _record(
                "match-newer",
                job="demo",
                task_type="backup",
                task_name="local",
                status=RunStatus.FAILED,
                origin=RunOrigin.MANUAL,
                finished_at=base + timedelta(minutes=3),
            )
        )
        await service.record(
            _record(
                "wrong-status",
                job="demo",
                task_type="backup",
                task_name="local",
                status=RunStatus.SUCCESS,
                origin=RunOrigin.MANUAL,
                finished_at=base + timedelta(minutes=2),
            )
        )
        await service.record(
            _record(
                "wrong-task",
                job="demo",
                task_type="workflow",
                task_name="nightly",
                status=RunStatus.FAILED,
                origin=RunOrigin.MANUAL,
                finished_at=base + timedelta(minutes=2),
            )
        )
        await service.record(
            _record(
                "match-older",
                job="demo",
                task_type="backup",
                task_name="local",
                status=RunStatus.FAILED,
                origin=RunOrigin.MANUAL,
                finished_at=base + timedelta(minutes=1),
            )
        )
        await service.record(
            _record(
                "wrong-job",
                job="other",
                task_type="backup",
                task_name="local",
                status=RunStatus.FAILED,
                origin=RunOrigin.MANUAL,
                finished_at=base,
            )
        )

        records = await service.list_history(
            limit=1,
            job="demo",
            task="backup.local",
            status="failed",
            origin="manual",
        )

        assert [record.run_id for record in records] == ["match-newer"]

    _run_async(scenario())


def test_list_filter_values_returns_distinct_history_values(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = RunHistoryService(tmp_path / "appdata.db")
        await service.record(
            _record(
                "manual",
                job="demo",
                task_type="backup",
                task_name="local",
                status=RunStatus.FAILED,
                origin=RunOrigin.MANUAL,
            )
        )
        await service.record(
            _record(
                "scheduler",
                job="other",
                task_type="workflow",
                task_name="nightly",
                status=RunStatus.SUCCESS,
                origin=RunOrigin.SCHEDULER,
            )
        )

        values = await service.list_filter_values()

        assert values == {
            "jobs": ["demo", "other"],
            "tasks": ["backup.local", "workflow.nightly"],
            "statuses": ["failed", "success"],
            "origins": ["manual", "scheduler"],
        }

    _run_async(scenario())


def test_list_finished_between_uses_exclusive_start_and_inclusive_end(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = RunHistoryService(tmp_path / "appdata.db")
        base = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        await service.record(_record("at-start", finished_at=base))
        await service.record(_record("inside", finished_at=base + timedelta(minutes=30)))
        await service.record(_record("at-end", finished_at=base + timedelta(hours=1)))
        await service.record(_record("after", finished_at=base + timedelta(hours=1, seconds=1)))

        records = await service.list_finished_between(
            after=base,
            before_or_at=base + timedelta(hours=1),
        )

        assert [record.run_id for record in records] == ["inside", "at-end"]

    _run_async(scenario())


def test_list_finished_between_excludes_active_runs(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = RunHistoryService(tmp_path / "appdata.db")
        base = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        await service.create_run(_record("running", status=RunStatus.RUNNING, finished_at=None))
        await service.record(_record("finished", finished_at=base + timedelta(minutes=1)))

        records = await service.list_finished_between(
            after=base,
            before_or_at=base + timedelta(hours=1),
        )

        assert [record.run_id for record in records] == ["finished"]

    _run_async(scenario())


def test_record_is_idempotent_upsert(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = RunHistoryService(tmp_path / "appdata.db")

        await service.record(_record("same", status=RunStatus.FAILED, error="old"))
        await service.record(
            _record(
                "same",
                status=RunStatus.SUCCESS,
                task_name="changed",
                error=None,
            )
        )

        records = await service.list_history()
        loaded = await service.get("same")
        assert [record.run_id for record in records] == ["same"]
        assert loaded is not None
        assert loaded.status == RunStatus.SUCCESS
        assert loaded.display_target == "demo.backup.changed"
        assert loaded.error is None

    _run_async(scenario())


def test_record_prunes_by_retention_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DK_APPDATA_RETENTION_COUNT", "2")
    monkeypatch.setenv("DK_APPDATA_RETENTION_DAYS", "3600")

    async def scenario() -> None:
        service = RunHistoryService(tmp_path / "appdata.db")
        base = datetime.now(timezone.utc)
        await service.record(_record("old", finished_at=base))
        await service.record(_record("newer", finished_at=base + timedelta(minutes=1)))
        await service.record(_record("newest", finished_at=base + timedelta(minutes=2)))

        assert [record.run_id for record in await service.list_history()] == [
            "newest",
            "newer",
        ]
        assert await service.get("old") is None

    _run_async(scenario())


def test_retention_count_preserves_active_runs_and_steps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DK_APPDATA_RETENTION_COUNT", "1")
    monkeypatch.setenv("DK_APPDATA_RETENTION_DAYS", "3600")

    async def scenario() -> None:
        service = RunHistoryService(tmp_path / "appdata.db")
        active_step_id: str
        await service.create_run(_record("active", status=RunStatus.RUNNING))
        active_step_id = await service.create_step(
            run_id="active",
            step="backup.local.backup",
            backend="restic",
            task_type="backup",
            task_name="local",
            effective_task_config={"repository": "/backups/local"},
        )
        await service.record(_record("old-terminal"))
        await service.record(_record("fresh-terminal", finished_at=datetime.now(timezone.utc)))

        active = await service.get("active")
        assert active is not None
        assert active.status == RunStatus.RUNNING
        assert await service.get("old-terminal") is None
        assert await service.get("fresh-terminal") is not None

        with closing(service._connect()) as conn:
            step = conn.execute(
                "SELECT run_id, status FROM run_steps WHERE run_step_id = ?",
                (active_step_id,),
            ).fetchone()
        assert step is not None
        assert step["run_id"] == "active"
        assert step["status"] == RunStatus.RUNNING.value

    _run_async(scenario())


def test_retention_count_prunes_only_job_task_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DK_APPDATA_RETENTION_COUNT", "1")
    monkeypatch.setenv("DK_APPDATA_RETENTION_DAYS", "3600")

    async def scenario() -> None:
        service = RunHistoryService(tmp_path / "appdata.db")
        base = datetime.now(timezone.utc)

        await service.record(_record("restore-old", run_kind=RunKind.RESTORE, finished_at=base))
        await service.record(
            _record(
                "restore-new",
                run_kind=RunKind.RESTORE,
                finished_at=base + timedelta(minutes=1),
            )
        )
        await service.record(_record("job-old", finished_at=base + timedelta(minutes=2)))
        await service.record(_record("job-new", finished_at=base + timedelta(minutes=3)))

        assert await service.get("job-old") is None
        assert await service.get("job-new") is not None
        assert await service.get("restore-old") is not None
        assert await service.get("restore-new") is not None

    _run_async(scenario())


def test_record_prunes_by_retention_days(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DK_APPDATA_RETENTION_COUNT", "100")
    monkeypatch.setenv("DK_APPDATA_RETENTION_DAYS", "1")

    async def scenario() -> None:
        service = RunHistoryService(tmp_path / "appdata.db")
        await service.record(
            _record("expired", finished_at=datetime.now(timezone.utc) - timedelta(days=2))
        )
        await service.record(_record("fresh", finished_at=datetime.now(timezone.utc)))

        assert [record.run_id for record in await service.list_history()] == ["fresh"]
        assert await service.get("expired") is None

    _run_async(scenario())


def test_retention_days_preserves_active_run_until_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DK_APPDATA_RETENTION_COUNT", "100")
    monkeypatch.setenv("DK_APPDATA_RETENTION_DAYS", "1")

    async def scenario() -> None:
        service = RunHistoryService(tmp_path / "appdata.db")
        await service.create_run(_record("active", status=RunStatus.RUNNING))
        await service.record(_record("fresh", finished_at=datetime.now(timezone.utc)))

        active = await service.get("active")
        assert active is not None
        assert active.status == RunStatus.RUNNING

        changed = service.mark_active_runs_interrupted_sync()
        interrupted = await service.get("active")
        assert changed == 1
        assert interrupted is not None
        assert interrupted.status == RunStatus.UNEXPECTED_ERROR

    _run_async(scenario())


def test_retention_keeps_run_linked_to_creation_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DK_APPDATA_RETENTION_COUNT", "1")
    monkeypatch.setenv("DK_APPDATA_RETENTION_DAYS", "1")

    async def scenario() -> None:
        db_path = tmp_path / "appdata.db"
        service = RunHistoryService(db_path)
        expired = datetime.now(timezone.utc) - timedelta(days=2)

        await service.create_run(_record("protected", status=RunStatus.RUNNING, finished_at=None))
        _insert_artifact_link(db_path, "protected", present=True)
        await service.record(_record("protected", finished_at=expired))
        await service.record(_record("fresh", finished_at=datetime.now(timezone.utc)))

        assert await service.get("protected") is not None
        assert await service.get("fresh") is not None

    _run_async(scenario())


def test_retention_prunes_run_linked_only_to_removed_creation_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DK_APPDATA_RETENTION_COUNT", "100")
    monkeypatch.setenv("DK_APPDATA_RETENTION_DAYS", "1")

    async def scenario() -> None:
        db_path = tmp_path / "appdata.db"
        service = RunHistoryService(db_path)
        expired = datetime.now(timezone.utc) - timedelta(days=2)

        await service.create_run(_record("expired", status=RunStatus.RUNNING, finished_at=None))
        _insert_artifact_link(db_path, "expired", present=False)
        await service.record(_record("expired", finished_at=expired))
        await service.record(_record("fresh", finished_at=datetime.now(timezone.utc)))

        assert await service.get("expired") is None
        assert await service.get("fresh") is not None
        with closing(service._connect()) as conn:
            row = conn.execute("""
                SELECT artifact_locations.removed_by_run_step_id,
                       repository_stats_points.run_step_id
                FROM artifact_locations
                CROSS JOIN repository_stats_points
                """).fetchone()
        assert row is not None
        assert row["removed_by_run_step_id"] is None
        assert row["run_step_id"] is None

    _run_async(scenario())


def test_retention_prunes_run_after_creation_artifact_is_hard_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DK_APPDATA_RETENTION_COUNT", "100")
    monkeypatch.setenv("DK_APPDATA_RETENTION_DAYS", "1")

    async def scenario() -> None:
        db_path = tmp_path / "appdata.db"
        service = RunHistoryService(db_path)

        await service.record(
            _record("expired", finished_at=datetime.now(timezone.utc) - timedelta(days=2))
        )
        await service.record(_record("fresh", finished_at=datetime.now(timezone.utc)))

        assert await service.get("expired") is None
        assert await service.get("fresh") is not None

    _run_async(scenario())


def _insert_artifact_link(db_path: Path, run_id: str, *, present: bool) -> None:
    with closing(RunHistoryService(db_path)._connect()) as conn:
        conn.execute(
            """
            INSERT INTO run_steps (
                run_step_id, run_id, position, step, backend, task_type, task_name,
                started_at, finished_at, status, error, effective_task_config_json
            )
            VALUES (
                'step-protected', ?, 0, 'backup.local.backup', 'restic', 'backup', 'local',
                '2026-01-01T00:00:00+00:00', '2026-01-01T00:01:00+00:00',
                'success', NULL, '{}'
            )
            """,
            (run_id,),
        )
        conn.execute("""
            INSERT INTO repositories (repository_id, backend, backend_repository_id)
            VALUES ('repo-1', 'restic', 'backend-repo-1')
            """)
        conn.execute("""
            INSERT INTO repository_locations (
                location_id, repository_id, repository_location_key, display_repository
            )
            VALUES ('location-1', 'repo-1', 'local:/repo', '/repo')
            """)
        conn.execute("""
            INSERT INTO artifacts (
                artifact_id, repository_id, backend, backend_artifact_id,
                created_at, created_run_step_id, paths_json, tags_json, raw_snapshot_json
            )
            VALUES (
                'artifact-1', 'repo-1', 'restic', 'snapshot-1',
                '2026-01-01T00:01:00+00:00', 'step-protected', '[]', '[]', '{}'
            )
            """)
        conn.execute(
            """
            INSERT INTO artifact_locations (
                artifact_location_id, artifact_id, location_id, present,
                removed_at, removed_by_run_step_id
            )
            VALUES (
                'artifact-location-1', 'artifact-1', 'location-1', ?,
                ?, ?
            )
            """,
            (
                1 if present else 0,
                None if present else "2026-01-02T00:00:00+00:00",
                None if present else "step-protected",
            ),
        )
        conn.execute("""
            INSERT INTO repository_stats_points (
                stats_point_id, repository_id, location_id, run_step_id, trigger,
                collected_at, total_size_bytes, artifacts_count, raw_stats_json
            )
            VALUES (
                'stats-1', 'repo-1', 'location-1', 'step-protected', 'backup',
                '2026-01-01T00:01:00+00:00', 1, 1, '{}'
            )
            """)


def test_active_runs_are_ignored(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = RunHistoryService(tmp_path / "appdata.db")

        await service.record(_record("queued", status=RunStatus.QUEUED, finished_at=None))
        await service.record(_record("running", status=RunStatus.RUNNING, finished_at=None))

        assert await service.get("queued") is None
        assert await service.get("running") is None
        assert await service.list_history() == []

    _run_async(scenario())


def test_missing_db_returns_empty_results(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = RunHistoryService(tmp_path / "missing" / "appdata.db")

        assert await service.get("missing") is None
        assert await service.list_history() == []

    _run_async(scenario())


def test_read_errors_are_swallowed_when_db_path_is_unusable(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = RunHistoryService(tmp_path)

        assert await service.get("missing") is None
        assert await service.list_history() == []

    _run_async(scenario())


def test_unknown_origin_or_status_is_skipped_when_reading(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = RunHistoryService(tmp_path / "appdata.db")
        await service.record(_record("valid"))

        def insert_invalid_rows() -> None:
            with closing(service._connect()) as conn:
                conn.execute(
                    """
                    INSERT INTO runs (
                        run_id, origin, run_kind, job, task_type, task_name,
                        started_at, finished_at, status, error, dry_run
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "bad-origin",
                        "unknown",
                        RunKind.JOB_TASK.value,
                        "demo",
                        "backup",
                        "bad",
                        "2026-01-01T12:00:00+00:00",
                        "2026-01-01T12:10:00+00:00",
                        RunStatus.SUCCESS.value,
                        None,
                        0,
                    ),
                )

        insert_invalid_rows()

        assert await service.get("bad-origin") is None
        assert [record.run_id for record in await service.list_history()] == ["valid"]

    _run_async(scenario())


def test_concurrent_writes_are_persisted(tmp_path: Path) -> None:
    db_path = tmp_path / "appdata.db"
    records = [_record(f"run-{index}") for index in range(20)]

    def write(record: RunRecord) -> None:
        _run_async(RunHistoryService(db_path).record(record))

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(write, record) for record in records]
        for future in futures:
            future.result()

    async def scenario() -> None:
        service = RunHistoryService(db_path)
        loaded = await service.list_history(limit=100)

        assert {record.run_id for record in loaded} == {record.run_id for record in records}

    _run_async(scenario())
