import asyncio
import json
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import ParamSpec, TypeVar
from unittest.mock import AsyncMock, patch

import pytest

from src.core.job_runner import BackupRunStatsContext
from src.core.locking import JobAlreadyRunningError, ResourceLockManager, resource_for_repository
from src.core.subprocesses import CommandResult
from src.services.appdata_schema import connect_appdata_db
from src.services.config import ConfigService
from src.services.database_inspection import DatabaseInspectionService
from src.services.errors import NotFoundServiceError, ServiceError
from src.services.logs import LogService, tail_file_lines
from src.services.rclone import RcloneService
from src.services.repositories import RepositoryService
from src.services.run_history import RunHistoryService
from src.services.run_manager import RunKind, RunManager, RunOrigin, RunRecord, RunStatus
from src.services.runs import RunService
from src.services.terminal_hooks import terminal_run_hook

P = ParamSpec("P")
T = TypeVar("T")


async def _wait_for_terminal(manager: RunManager, run_id: str) -> None:
    for _ in range(200):
        try:
            record = await manager.get(run_id)
        except NotFoundServiceError:
            return
        if record.status not in {RunStatus.QUEUED, RunStatus.RUNNING}:
            return
        await asyncio.sleep(0.001)
    raise AssertionError("run did not become terminal")


@pytest.fixture(autouse=True)
def run_to_thread_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def immediate(func: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", immediate)


def test_log_service_lists_reads_raw_logs_and_validates_paths(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    job_dir = log_dir / "demo"
    job_dir.mkdir(parents=True)
    (job_dir / "2026-05-25.log").write_text("ok secret-value\n", encoding="utf-8")
    (job_dir / "not-a-date.log").write_text("ignored\n", encoding="utf-8")
    (log_dir / "../escape").mkdir(exist_ok=True)

    service = LogService(log_dir)

    assert service.list_jobs() == {"jobs": [{"name": "demo", "dates": ["2026-05-25"]}]}
    assert service.list_job("demo") == {"job": "demo", "dates": ["2026-05-25"]}
    raw = asyncio.run(service.read_raw("demo", "2026-05-25"))
    assert raw["content"] == "ok secret-value\n"

    with pytest.raises(ServiceError, match="Invalid job name"):
        service.list_job("../demo")
    with pytest.raises(ServiceError, match="Date must be YYYY-MM-DD"):
        asyncio.run(service.read_raw("demo", "../2026-05-25"))
    with pytest.raises(ServiceError, match="Log file not found"):
        asyncio.run(service.read_raw("demo", "2026-05-24"))


def test_log_service_tail_raw_reads_bounded_without_read_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_dir = tmp_path / "logs"
    job_dir = log_dir / "demo"
    job_dir.mkdir(parents=True)
    log_path = job_dir / "2026-05-25.log"
    very_long = "x" * 10_000
    log_path.write_text(f"first\n{very_long}\nlast", encoding="utf-8")

    def fail_read_text(self: Path, *args: object, **kwargs: object) -> str:
        raise AssertionError(f"read_text must not be used for tailed logs: {self}")

    monkeypatch.setattr(Path, "read_text", fail_read_text)

    service = LogService(log_dir)
    raw = asyncio.run(service.read_raw("demo", "2026-05-25", tail=2))

    assert raw["content"] == f"{very_long}\nlast"
    assert raw["truncated"] is True


def test_log_service_get_logs_view_selects_exact_job_with_similar_names(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    local_dir = log_dir / "local"
    locale_dir = log_dir / "locale_section"
    local_dir.mkdir(parents=True)
    locale_dir.mkdir()
    (local_dir / "2026-05-25.log").write_text("local only\n", encoding="utf-8")
    (locale_dir / "2026-05-25.log").write_text("locale section only\n", encoding="utf-8")

    service = LogService(log_dir)
    view = asyncio.run(service.get_logs_view("local", "2026-05-25"))

    assert view["selected_job"] == "local"
    assert view["log_content"] == "local only\n"


def test_log_service_get_logs_view_reports_applied_tail(tmp_path: Path) -> None:
    """The view states its own tail so the client can refresh the same window."""
    log_dir = tmp_path / "logs"
    job_dir = log_dir / "local"
    job_dir.mkdir(parents=True)
    (job_dir / "2026-05-25.log").write_text("one\ntwo\n", encoding="utf-8")

    service = LogService(log_dir)
    view = asyncio.run(service.get_logs_view("local", "2026-05-25"))

    assert view["log_tail"] == LogService._STATIC_TAIL
    assert view["log_truncated"] is False


def test_tail_file_lines_handles_zero_and_single_line(tmp_path: Path) -> None:
    log_path = tmp_path / "demo.log"
    log_path.write_text("first\nsecond\n", encoding="utf-8")

    assert tail_file_lines(log_path, 0) == ([], False)
    assert tail_file_lines(log_path, 1) == (["second\n"], True)


def test_log_service_exposes_system_log_directory(tmp_path: Path) -> None:
    """Der System-Logger schreibt nach ``_system/`` und ist über die GUI lesbar."""
    from src.utils.logging import SYSTEM_LOG_NAME

    log_dir = tmp_path / "logs"
    system_dir = log_dir / SYSTEM_LOG_NAME
    system_dir.mkdir(parents=True)
    (system_dir / "2026-05-25.log").write_text("scheduler started\n", encoding="utf-8")

    service = LogService(log_dir)

    listed = service.list_jobs()["jobs"]
    assert {"name": SYSTEM_LOG_NAME, "dates": ["2026-05-25"]} in listed
    raw = asyncio.run(service.read_raw(SYSTEM_LOG_NAME, "2026-05-25"))
    assert raw["content"] == "scheduler started\n"


def test_log_service_open_stream_validates_before_iteration(tmp_path: Path) -> None:
    service = LogService(tmp_path)

    with pytest.raises(ServiceError, match="Invalid job name"):
        service.open_stream("../demo")

    stream = service.open_stream("demo")
    assert hasattr(stream, "__aiter__")


def test_log_service_open_stream_tails_existing_raw_logs(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    job_dir = log_dir / "demo"
    job_dir.mkdir(parents=True)
    (job_dir / "2026-05-25.log").write_text("first\nsecond secret-value\n", encoding="utf-8")
    service = LogService(log_dir)

    async def first_event() -> str:
        stream = service.open_stream("demo", tail=1)
        return await anext(stream)

    assert asyncio.run(first_event()) == 'data: {"line": "second secret-value"}\n\n'


def test_log_service_open_stream_initial_tail_avoids_read_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_dir = tmp_path / "logs"
    job_dir = log_dir / "demo"
    job_dir.mkdir(parents=True)
    (job_dir / "2026-05-25.log").write_text("first\nsecond\n", encoding="utf-8")
    service = LogService(log_dir)

    def fail_read_text(self: Path, *args: object, **kwargs: object) -> str:
        raise AssertionError(f"read_text must not be used for stream tails: {self}")

    monkeypatch.setattr(Path, "read_text", fail_read_text)

    async def first_event() -> str:
        stream = service.open_stream("demo", tail=1)
        return await anext(stream)

    assert asyncio.run(first_event()) == 'data: {"line": "second"}\n\n'


def test_log_service_open_stream_stops_if_client_disconnects_before_first_log(
    tmp_path: Path,
) -> None:
    service = LogService(tmp_path / "logs")
    is_disconnected = AsyncMock(return_value=True)

    async def consume() -> None:
        stream = service.open_stream("demo", is_disconnected=is_disconnected)
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(stream), timeout=0.1)

    asyncio.run(consume())
    is_disconnected.assert_awaited_once()


def test_log_service_open_stream_stops_if_client_disconnects_after_log_disappears(
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "logs"
    job_dir = log_dir / "demo"
    job_dir.mkdir(parents=True)
    log_path = job_dir / "2026-05-25.log"
    log_path.write_text("first\n", encoding="utf-8")
    service = LogService(log_dir)
    is_disconnected = AsyncMock(side_effect=[False, True])

    async def sleep_and_remove_log(_seconds: float) -> None:
        if log_path.exists():
            log_path.unlink()

    async def consume() -> None:
        stream = service.open_stream("demo", is_disconnected=is_disconnected)
        first_event = await asyncio.wait_for(anext(stream), timeout=0.1)
        assert first_event == 'data: {"line": "first"}\n\n'
        with patch("src.services.logs.asyncio.sleep", AsyncMock(side_effect=sleep_and_remove_log)):
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(anext(stream), timeout=0.1)

    asyncio.run(consume())
    assert is_disconnected.await_count == 2


def test_log_service_open_stream_follows_newer_daily_log_after_rollover(
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "logs"
    job_dir = log_dir / "demo"
    job_dir.mkdir(parents=True)
    old_log = job_dir / "2026-05-25.log"
    new_log = job_dir / "2026-05-26.log"
    old_log.write_text("first\n", encoding="utf-8")
    service = LogService(log_dir)

    async def sleep_and_rollover(_seconds: float) -> None:
        if not new_log.exists():
            with old_log.open("a", encoding="utf-8") as handle:
                handle.write("last old line\n")
            new_log.write_text("first new line\n", encoding="utf-8")

    async def consume() -> None:
        stream = service.open_stream("demo", tail=1)
        first_event = await asyncio.wait_for(anext(stream), timeout=0.1)
        assert first_event == 'data: {"line": "first"}\n\n'
        with patch("src.services.logs.asyncio.sleep", AsyncMock(side_effect=sleep_and_rollover)):
            old_event = await asyncio.wait_for(anext(stream), timeout=0.1)
            new_event = await asyncio.wait_for(anext(stream), timeout=0.1)
        assert old_event == 'data: {"line": "last old line"}\n\n'
        assert new_event == 'data: {"line": "first new line"}\n\n'

    asyncio.run(consume())


def test_database_inspection_missing_db_is_read_only(tmp_path: Path) -> None:
    db_path = tmp_path / "missing" / "appdata.db"
    service = DatabaseInspectionService(db_path=db_path)

    view = asyncio.run(service.get_database_view())

    assert view["db"]["exists"] is False
    assert view["integrity"] == "missing"
    assert not db_path.exists()
    assert not db_path.parent.exists()


def test_database_inspection_lists_whitelisted_rows_and_counts(tmp_path: Path) -> None:
    db_path = tmp_path / "appdata.db"
    with closing(connect_appdata_db(db_path)) as conn:
        conn.executemany(
            """
            INSERT INTO runs (
                run_id, origin, run_kind, job, task_type, task_name,
                started_at, finished_at, status, dry_run
            )
            VALUES (?, 'manual', 'job_task', 'demo', 'backup', 'local',
                    ?, ?, 'success', 0)
            """,
            [
                ("run-1", "2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z"),
                ("run-2", "2026-01-02T00:00:00Z", "2026-01-02T00:01:00Z"),
            ],
        )

    service = DatabaseInspectionService(db_path=db_path)

    view = asyncio.run(service.get_database_view(section="runs", table="runs"))

    assert view["db"]["exists"] is True
    assert view["integrity"] == "ok"
    assert view["active_section"] == "runs"
    table = view["table"]
    assert table["name"] == "runs"
    assert table["columns"] == [
        "run_id",
        "origin",
        "run_kind",
        "job",
        "task_type",
        "task_name",
        "started_at",
        "finished_at",
        "status",
        "error",
        "dry_run",
    ]
    assert table["total"] == 2
    assert [row["run_id"] for row in table["rows"]] == ["run-2", "run-1"]
    assert view["data_model"]


def test_database_inspection_system_meta_table_is_explicitly_viewable(tmp_path: Path) -> None:
    db_path = tmp_path / "appdata.db"
    with closing(connect_appdata_db(db_path)) as conn:
        conn.execute("""
            INSERT INTO appdata_meta (key, value)
            VALUES ('repository_artifact_retention_date:repo-1', '2026-07-01')
            """)

    service = DatabaseInspectionService(db_path=db_path)

    overview = asyncio.run(service.get_database_view())
    table_view = asyncio.run(service.get_database_view(section="system", table="appdata_meta"))

    assert overview["show_system_overview"] is True
    assert table_view["show_system_overview"] is False
    assert table_view["table"]["name"] == "appdata_meta"
    assert table_view["table"]["rows"] == [
        {"key": "repository_artifact_retention_date:repo-1", "value": "2026-07-01"},
        {"key": "schema_generation", "value": "run_mode_v1"},
    ]


def test_database_inspection_filters_rows_and_exposes_cell_links(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "appdata.db"
    with closing(connect_appdata_db(db_path)) as conn:
        conn.execute("""
            INSERT INTO repositories (repository_id, backend, backend_repository_id)
            VALUES ('repo-1', 'restic', 'backend-repo-1')
            """)
        conn.executemany(
            """
            INSERT INTO artifacts (
                artifact_id, repository_id, backend, backend_artifact_id,
                backend_artifact_short_id, created_at, paths_json, tags_json,
                raw_snapshot_json, raw_backup_output_json
            )
            VALUES (?, 'repo-1', 'restic', ?, ?, ?, '[]', '[]', '{}', ?)
            """,
            [
                (
                    "artifact-1",
                    "snapshot-1",
                    "snap-1",
                    "2026-01-01T00:00:00Z",
                    '{"files":["' + ("x" * 120) + '"]}',
                ),
                (
                    "artifact-2",
                    "snapshot-2",
                    "snap-2",
                    "2026-01-02T00:00:00Z",
                    None,
                ),
            ],
        )

    service = DatabaseInspectionService(db_path=db_path)

    view = asyncio.run(
        service.get_database_view(
            section="artifacts",
            table="artifacts",
            filter_column="artifact_id",
            filter_value="artifact-1",
        )
    )

    table = view["table"]
    assert table["total"] == 1
    assert table["filter"] == {"column": "artifact_id", "value": "artifact-1"}
    assert table["rows"][0]["artifact_id"] == "artifact-1"
    artifact_cell = table["row_cells"][0][0]
    assert artifact_cell["link"] == (
        "/diagnostics/database?section=artifacts&table=artifacts&"
        "filter_column=artifact_id&filter_value=artifact-1"
    )
    nav_items = {item["name"]: item for group in view["groups"] for item in group["tables"]}
    # A table that has the filtered column keeps the filter in link and count...
    assert nav_items["artifact_locations"]["url"] == (
        "/diagnostics/database?section=artifacts&table=artifact_locations&page=1&"
        "filter_column=artifact_id&filter_value=artifact-1"
    )
    assert nav_items["artifacts"]["count"] == 1
    # ...a table without it is offered unfiltered, with its full row count.
    assert nav_items["runs"]["url"] == ("/diagnostics/database?section=runs&table=runs&page=1")
    assert nav_items["runs"]["count"] == 0

    summary_view = asyncio.run(
        service.get_database_view(
            section="artifacts",
            table="artifacts",
            filter_column="artifact_id",
            filter_value="artifact-1",
        )
    )
    summary_table = summary_view["table"]
    json_column = summary_table["columns"].index("raw_backup_output_json")
    json_cell = summary_table["row_cells"][0][json_column]
    assert json_cell["is_long"] is True
    assert str(json_cell["preview"]).endswith("...")


def test_database_inspection_links_current_run_model_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "appdata.db"
    with closing(connect_appdata_db(db_path)) as conn:
        conn.execute("""
            INSERT INTO runs (
                run_id, origin, run_kind, job, task_type, task_name,
                started_at, status, dry_run
            )
            VALUES (
                'run-1', 'manual', 'job_task', 'demo', 'backup', 'local',
                '2026-01-01T00:00:00Z', 'running', 0
            )
            """)
        conn.execute("""
            INSERT INTO run_steps (
                run_step_id, run_id, position, step, backend, task_type, task_name,
                started_at, status, effective_task_config_json
            )
            VALUES (
                'step-1', 'run-1', 0, 'backup', 'restic', 'backup', 'local',
                '2026-01-01T00:00:00Z', 'running', '{}'
            )
            """)
        conn.execute("""
            INSERT INTO run_restores (
                run_restore_id, run_id, job, backup, backend, snapshot_id, mode,
                restore_target, snapshot_paths_json, include_patterns_json,
                exclude_patterns_json, overwrite, output_truncated
            )
            VALUES (
                'restore-1', 'run-1', 'demo', 'local', 'restic', 'snapshot-1',
                'pattern', '/restore/demo/local', '[]', '[]', '[]', 0, 0
            )
            """)
        conn.execute("""
            INSERT INTO repositories (repository_id, backend, backend_repository_id)
            VALUES ('repo-1', 'restic', 'backend-repo-1')
            """)
        conn.execute("""
            INSERT INTO artifacts (
                artifact_id, repository_id, backend, backend_artifact_id,
                created_at, created_run_step_id, paths_json, tags_json,
                raw_snapshot_json
            )
            VALUES (
                'artifact-1', 'repo-1', 'restic', 'snapshot-1',
                '2026-01-01T00:01:00Z', 'step-1', '[]', '[]', '{}'
            )
            """)

    service = DatabaseInspectionService(db_path=db_path)

    steps_view = asyncio.run(service.get_database_view(section="runs", table="run_steps"))
    step_cells = steps_view["table"]["row_cells"][0]
    step_columns = steps_view["table"]["columns"]
    assert step_cells[step_columns.index("run_step_id")]["link"] == (
        "/diagnostics/database?section=runs&table=run_steps&"
        "filter_column=run_step_id&filter_value=step-1"
    )
    assert step_cells[step_columns.index("run_id")]["link"] == (
        "/diagnostics/database?section=runs&table=run_steps&"
        "filter_column=run_id&filter_value=run-1"
    )

    restores_view = asyncio.run(service.get_database_view(section="restores", table="run_restores"))
    restore_cells = restores_view["table"]["row_cells"][0]
    restore_columns = restores_view["table"]["columns"]
    assert restore_cells[restore_columns.index("run_restore_id")]["link"] == (
        "/diagnostics/database?section=restores&table=run_restores&"
        "filter_column=run_restore_id&filter_value=restore-1"
    )
    assert restore_cells[restore_columns.index("run_id")]["link"] == (
        "/diagnostics/database?section=restores&table=run_restores&"
        "filter_column=run_id&filter_value=run-1"
    )

    artifacts_view = asyncio.run(service.get_database_view(section="artifacts", table="artifacts"))
    artifact_cells = artifacts_view["table"]["row_cells"][0]
    artifact_columns = artifacts_view["table"]["columns"]
    assert artifact_cells[artifact_columns.index("created_run_step_id")]["link"] == (
        "/diagnostics/database?section=runs&table=run_steps&"
        "filter_column=run_step_id&filter_value=step-1"
    )


def test_database_inspection_ignores_unknown_filter_column(tmp_path: Path) -> None:
    db_path = tmp_path / "appdata.db"
    with closing(connect_appdata_db(db_path)) as conn:
        conn.execute("""
            INSERT INTO runs (
                run_id, origin, run_kind, job, task_type, task_name,
                started_at, finished_at, status, dry_run
            )
            VALUES (
                'run-1', 'manual', 'job_task', 'demo', 'backup', 'local',
                '2026-01-01T00:00:00Z', '2026-01-01T00:01:00Z', 'success', 0
            )
            """)

    service = DatabaseInspectionService(db_path=db_path)

    view = asyncio.run(
        service.get_database_view(
            section="runs",
            table="runs",
            filter_column="not_a_column",
            filter_value="run-1",
        )
    )

    table = view["table"]
    assert table["filter"] is None
    assert table["total"] == 1


def test_database_inspection_whitelist_uses_only_current_appdata_tables() -> None:
    from src.services.database_inspection import TABLES

    assert {table.name for table in TABLES} == {
        "appdata_meta",
        "repositories",
        "repository_locations",
        "runs",
        "run_steps",
        "run_restores",
        "artifacts",
        "artifact_locations",
        "repository_stats_points",
    }
    assert {table.name: table.order_by for table in TABLES} == {
        "appdata_meta": "key ASC",
        "repositories": "backend ASC, backend_repository_id ASC",
        "repository_locations": "repository_location_key ASC, location_id DESC",
        "runs": "COALESCE(finished_at, started_at) DESC, run_id DESC",
        "run_steps": "run_id DESC, position ASC",
        "run_restores": "run_id DESC, run_restore_id DESC",
        "artifacts": "created_at DESC, artifact_id DESC",
        "artifact_locations": "present DESC, artifact_id DESC",
        "repository_stats_points": "collected_at DESC, stats_point_id DESC",
    }


def test_rclone_service_reads_saves_and_returns_raw_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "rclone.conf"
    monkeypatch.setenv("RCLONE_CONFIG", str(config_path))
    service = RcloneService(tmp_path)

    missing_view = asyncio.run(service.get_rclone_view())
    assert missing_view["conf_path"] == str(config_path)
    assert missing_view["content"] == ""
    assert missing_view["conf_missing"] is True
    assert missing_view["error"] is None

    asyncio.run(
        service.save_config("[drive]\ntype = drive\ntoken = secret-token\n[--bad]\ntype = s3\n")
    )

    with patch("src.services.rclone.run_command", AsyncMock()) as run_mock:
        view = asyncio.run(service.get_rclone_view())

    assert view["content"] == "[drive]\ntype = drive\ntoken = secret-token\n[--bad]\ntype = s3\n"
    assert view["remotes"] == ["drive"]
    assert view["remote_types"] == {"drive": "drive"}
    run_mock.assert_not_awaited()

    lsd_result = CommandResult(1, stdout="", stderr="failed secret-token")
    with patch("src.services.rclone.run_command", AsyncMock(return_value=lsd_result)):
        diagnostic = asyncio.run(service.test_remote("drive"))

    assert diagnostic["ok"] is False
    assert diagnostic["message"] == "not reachable"
    assert diagnostic["output"] == "failed secret-token"

    with patch(
        "src.services.rclone.run_command",
        AsyncMock(side_effect=asyncio.TimeoutError),
    ):
        assert asyncio.run(service.test_remote("drive"))["message"] == "timeout"

    with pytest.raises(ServiceError, match="Invalid rclone remote name"):
        asyncio.run(service.test_remote("--config=/tmp/x"))


def test_rclone_remote_view_success_runs_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "rclone.conf"
    monkeypatch.setenv("RCLONE_CONFIG", str(config_path))
    config_path.write_text("[drive]\ntype = drive\n", encoding="utf-8")
    service = RcloneService(tmp_path)

    lsd_result = CommandResult(0, stdout="ok", stderr="")
    with patch("src.services.rclone.run_command", AsyncMock(return_value=lsd_result)) as run_mock:
        view = asyncio.run(service.test_remote_view("drive"))

    run_mock.assert_awaited_once_with(["rclone", "lsd", "drive:"], timeout=15)
    assert view == {
        "status": "ok",
        "tone": "success",
        "detail": None,
        "symbol": "OK",
        "label": "Reachable",
    }


def test_rclone_remote_view_timeout(tmp_path: Path) -> None:
    service = RcloneService(tmp_path)

    with patch(
        "src.services.rclone.run_command",
        AsyncMock(side_effect=asyncio.TimeoutError),
    ):
        view = asyncio.run(service.test_remote_view("drive"))

    assert view == {
        "status": "warning",
        "tone": "warning",
        "detail": None,
        "symbol": "!",
        "label": "Timeout",
    }


def test_rclone_remote_view_failure(tmp_path: Path) -> None:
    service = RcloneService(tmp_path)

    lsd_result = CommandResult(1, stdout="", stderr="connection refused")
    with patch("src.services.rclone.run_command", AsyncMock(return_value=lsd_result)):
        view = asyncio.run(service.test_remote_view("drive"))

    assert view == {
        "status": "error",
        "tone": "danger",
        "detail": "not reachable",
        "symbol": "X",
        "label": "Not reachable",
    }


def test_rclone_service_view_keeps_oserror_out_of_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "rclone.conf"
    monkeypatch.setenv("RCLONE_CONFIG", str(config_path))
    service = RcloneService(tmp_path)

    with patch.object(Path, "read_text", side_effect=OSError("denied secret-token")):
        result = asyncio.run(service.get_rclone_view())

    assert result["content"] == ""
    assert result["conf_missing"] is True
    assert result["error"] == {"code": "read_error", "message": "denied secret-token"}


def test_rclone_config_path_uses_env_or_config_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RCLONE_CONFIG", raising=False)
    service = RcloneService(tmp_path)
    assert service.config_path() == tmp_path / "rclone.conf"

    override = tmp_path / "custom.conf"
    monkeypatch.setenv("RCLONE_CONFIG", str(override))
    assert service.config_path() == override


def test_rclone_create_validates_required_fields_before_command(tmp_path: Path) -> None:
    service = RcloneService(tmp_path)

    with patch("src.services.rclone.run_command", AsyncMock()) as run_mock:
        result = asyncio.run(service.create_remote("nas", "sftp", {"user": "backup"}))

    assert result["result"]["ok"] is False
    assert result["result"]["errors"] == {"host": "Required field."}
    run_mock.assert_not_awaited()


def test_rclone_create_builds_command_and_obscures_rclone_password(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "rclone.conf"
    monkeypatch.setenv("RCLONE_CONFIG", str(config_path))
    service = RcloneService(tmp_path)
    command_result = CommandResult(0, stdout="created", stderr="")

    with patch(
        "src.services.rclone.run_command",
        AsyncMock(return_value=command_result),
    ) as run_mock:
        result = asyncio.run(
            service.create_remote(
                "nas",
                "sftp",
                {"host": "example.com", "user": "backup", "pass": "secret"},
            )
        )

    assert result["result"]["ok"] is True
    argv = run_mock.call_args.args[0]
    assert argv == [
        "rclone",
        "config",
        "create",
        "nas",
        "sftp",
        "host",
        "example.com",
        "user",
        "backup",
        "pass",
        "secret",
        "--non-interactive",
        "--obscure",
    ]
    assert "--config" not in argv


def test_rclone_create_does_not_obscure_plain_secret_fields(tmp_path: Path) -> None:
    service = RcloneService(tmp_path)
    command_result = CommandResult(0, stdout="created", stderr="")

    with patch(
        "src.services.rclone.run_command",
        AsyncMock(return_value=command_result),
    ) as run_mock:
        asyncio.run(
            service.create_remote(
                "s3remote",
                "s3",
                {
                    "provider": "Wasabi",
                    "access_key_id": "key",
                    "secret_access_key": "secret",
                    "endpoint": "https://s3.example.com",
                },
            )
        )

    argv = run_mock.call_args.args[0]
    assert "secret_access_key" in argv
    assert "--obscure" not in argv


def test_rclone_create_form_preserves_values_when_backend_changes(tmp_path: Path) -> None:
    service = RcloneService(tmp_path)

    form = asyncio.run(
        service.get_remote_form(
            None,
            "ftp",
            {
                "name": "offsite",
                "host": "example.com",
                "user": "backup",
                "port": "2222",
                "pass": "secret",
            },
        )
    )
    values = {field["name"]: field["value"] for field in form["fields"]}

    assert form["remote_name"] == "offsite"
    assert values["host"] == "example.com"
    assert values["user"] == "backup"
    assert values["port"] == "2222"
    assert values["pass"] == "secret"


def test_rclone_update_skips_empty_fields_and_passwords(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "rclone.conf"
    monkeypatch.setenv("RCLONE_CONFIG", str(config_path))
    config_path.write_text(
        "[nas]\ntype = sftp\nhost = old.example\npass = obscured\n",
        encoding="utf-8",
    )
    service = RcloneService(tmp_path)
    command_result = CommandResult(0, stdout="updated", stderr="")

    with patch(
        "src.services.rclone.run_command",
        AsyncMock(return_value=command_result),
    ) as run_mock:
        asyncio.run(service.update_remote("nas", {"host": "new.example", "pass": ""}))

    assert run_mock.call_args.args[0] == [
        "rclone",
        "config",
        "update",
        "nas",
        "host",
        "new.example",
        "--non-interactive",
    ]


def test_rclone_edit_form_omits_rclone_password_but_keeps_plain_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "rclone.conf"
    monkeypatch.setenv("RCLONE_CONFIG", str(config_path))
    config_path.write_text(
        "[s3remote]\ntype = s3\nprovider = Wasabi\nsecret_access_key = clear\n",
        encoding="utf-8",
    )
    service = RcloneService(tmp_path)

    form = asyncio.run(service.get_remote_form("s3remote", "s3"))
    values = {field["name"]: field["value"] for field in form["fields"]}

    assert values["secret_access_key"] == "clear"

    config_path.write_text(
        "[nas]\ntype = sftp\nhost = example\npass = obscured\n",
        encoding="utf-8",
    )
    form = asyncio.run(service.get_remote_form("nas", "sftp"))
    values = {field["name"]: field["value"] for field in form["fields"]}
    assert values["pass"] == ""


def _write_repo_config(path: Path) -> None:
    path.write_text(
        """
[jobs.demo.backup.local]
repository = "/repo/secret-value"
sources = ["/data"]
password = "secret-value"
""".strip(),
        encoding="utf-8",
    )


def test_repository_service_returns_raw_snapshots_and_stats(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_repo_config(config_path)
    service = RepositoryService(ConfigService(config_path))

    snapshots_result = CommandResult(
        0, stdout='[{"id": "abc", "paths": ["/data/secret-value"]}]', stderr=""
    )
    stats_result = CommandResult(0, stdout='{"total_size": 123, "note": "secret-value"}', stderr="")

    with patch(
        "src.services.repositories.run_command",
        AsyncMock(side_effect=[snapshots_result, stats_result]),
    ):
        snapshots = service.snapshots("demo", "local")
        stats = service.stats("demo", "local")

    assert snapshots["repository"] == "/repo/secret-value"
    assert snapshots["snapshots"] == [{"id": "abc", "paths": ["/data/secret-value"]}]
    assert stats["available"] is True
    assert stats["stats"] == {"total_size": 123, "note": "secret-value"}


def test_repository_service_snapshots_latest_builds_latest_command(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_repo_config(config_path)
    service = RepositoryService(ConfigService(config_path))
    snapshots_result = CommandResult(0, stdout="[]", stderr="")

    with patch(
        "src.services.repositories.run_command",
        AsyncMock(return_value=snapshots_result),
    ) as run:
        snapshots = service.snapshots("demo", "local", latest=1)

    assert snapshots["snapshots"] == []
    cmd = run.call_args.args[0]
    assert cmd == [
        "restic",
        "--repo",
        "/repo/secret-value",
        "snapshots",
        "--latest",
        "1",
        "--json",
    ]


def test_repository_service_handles_failures_and_missing_backup(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_repo_config(config_path)
    service = RepositoryService(ConfigService(config_path))

    with patch(
        "src.services.repositories.run_command",
        AsyncMock(side_effect=OSError("restic missing")),
    ):
        stats = service.stats("demo", "local")

    assert stats["available"] is False
    assert stats["error"] == "restic command failed: restic missing"

    with pytest.raises(ServiceError, match="Backup not found"):
        service.stats("demo", "missing")


def test_repository_service_rejects_invalid_names(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_repo_config(config_path)
    service = RepositoryService(ConfigService(config_path))

    with pytest.raises(ServiceError) as job_exc:
        service.snapshots("../demo", "local")
    with pytest.raises(ServiceError) as backup_exc:
        service.snapshots("demo", "../local")

    assert job_exc.value.code == "invalid_parameter"
    assert backup_exc.value.code == "invalid_parameter"


def test_repository_stats_preserves_returncode_timeout_and_json_errors(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_repo_config(config_path)
    service = RepositoryService(ConfigService(config_path))
    failed = CommandResult(2, stdout="stdout secret-value", stderr="stderr secret-value")
    invalid_json = CommandResult(0, stdout="{", stderr="")

    with patch("src.services.repositories.run_command", AsyncMock(return_value=failed)):
        failed_stats = service.stats("demo", "local")
    with patch(
        "src.services.repositories.run_command",
        AsyncMock(side_effect=asyncio.TimeoutError),
    ):
        timeout_stats = service.stats(
            "demo",
            "local",
            timeout=75,
            timeout_env_hint="DK_STATS_TIMEOUT",
        )
    with patch("src.services.repositories.run_command", AsyncMock(return_value=invalid_json)):
        json_stats = service.stats("demo", "local")

    assert failed_stats["available"] is False
    assert "restic exited with 2" in str(failed_stats["error"])
    assert "secret-value" in str(failed_stats["error"])
    assert "timed out" in str(timeout_stats["error"])
    assert "DK_STATS_TIMEOUT" in str(timeout_stats["error"])
    assert "invalid restic JSON output" in str(json_stats["error"])


def test_repository_service_defaults_to_stats_timeout_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    _write_repo_config(config_path)
    service = RepositoryService(ConfigService(config_path))
    monkeypatch.setenv("DK_STATS_TIMEOUT", "75")
    result = CommandResult(0, stdout="[]", stderr="")

    with patch("src.services.repositories.run_command", AsyncMock(return_value=result)) as run:
        snapshots = service.snapshots("demo", "local")

    assert snapshots["error"] is None
    assert run.await_args.kwargs["timeout"] == 75


def test_repository_stats_rejects_successful_non_object_json(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_repo_config(config_path)
    service = RepositoryService(ConfigService(config_path))
    result = CommandResult(0, stdout="[]", stderr="")

    with patch("src.services.repositories.run_command", AsyncMock(return_value=result)):
        stats = service.stats("demo", "local")

    assert stats["available"] is False
    assert stats["stats"] == {}
    assert stats["error"] == "unexpected restic stats response"


def test_repository_snapshots_rejects_successful_non_list_json(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_repo_config(config_path)
    service = RepositoryService(ConfigService(config_path))
    result = CommandResult(0, stdout="{}", stderr="")

    with patch("src.services.repositories.run_command", AsyncMock(return_value=result)):
        snapshots = service.snapshots("demo", "local")

    assert snapshots["snapshots"] == []
    assert snapshots["error"] == "unexpected restic snapshots response"


def test_run_service_uses_trigger_config_snapshot_and_tracks_task(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_repo_config(config_path)
    manager = RunManager()
    service = RunService(
        ConfigService(config_path),
        manager,
        tmp_path / "locks",
        tmp_path / "logs",
    )

    async def scenario() -> None:
        with patch("src.services.runs.JobRunner") as runner_cls:
            runner_cls.return_value.run_backup = AsyncMock(return_value=True)
            record = await service.start_run("demo.backup.local", dry_run=True)
            config_path.write_text(
                '[jobs.other.backup.local]\nrepository = "/repo"\nsources = ["/data"]\n',
                encoding="utf-8",
            )
            await asyncio.sleep(0.05)

        assert runner_cls.call_args.kwargs["dry_run"] is True
        assert runner_cls.call_args.args[0] == "demo"
        assert "local" in runner_cls.call_args.args[1].backup
        assert record.status.value in {"running", "success"}

    asyncio.run(scenario())


def test_run_service_displays_backup_substep_in_type_label(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_repo_config(config_path)
    manager = RunManager()
    service = RunService(
        ConfigService(config_path),
        manager,
        tmp_path / "locks",
        tmp_path / "logs",
    )

    async def scenario() -> None:
        with patch("src.services.runs.JobRunner") as runner_cls:
            runner_cls.return_value.run_step = AsyncMock(return_value=True)
            record = await service.start_run("demo.backup.local.cleanup")
            await asyncio.sleep(0)

        view = await service.get_run_view(record.run_id)

        runner_cls.return_value.run_step.assert_awaited_once_with("backup.local.cleanup")
        assert view["target"] == "demo.backup.local.cleanup"
        assert view["target_primary"] == "demo: local"
        assert view["task_name"] == "local"
        assert view["task_type_label"] == "Backup (cleanup)"
        assert view["task_substep_label"] == "cleanup"

    asyncio.run(scenario())


def test_run_manager_holds_task_until_completion(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_repo_config(config_path)
    manager = RunManager()
    service = RunService(
        ConfigService(config_path),
        manager,
        tmp_path / "locks",
        tmp_path / "logs",
    )

    async def slow_run(*_args: object) -> bool:
        await asyncio.sleep(0.05)
        return True

    async def scenario() -> None:
        with patch("src.services.runs._run_parsed_task_selector", side_effect=slow_run):
            record = await service.start_run("demo.backup.local")
            assert len(await manager.list()) == 1
            for _ in range(20):
                if len(await manager.list()) == 0:
                    break
                await asyncio.sleep(0.02)

        assert await manager.list() == []
        assert record.status.value == "success"

    asyncio.run(scenario())


def test_run_service_cancels_active_manual_run(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_repo_config(config_path)
    manager = RunManager()
    service = RunService(ConfigService(config_path), manager, tmp_path / "locks", tmp_path / "logs")

    async def wait_forever(*_args: object) -> bool:
        await asyncio.Event().wait()
        return True

    async def scenario() -> None:
        with patch("src.services.runs._run_parsed_task_selector", side_effect=wait_forever):
            record = await service.start_run("demo.backup.local")
            await asyncio.sleep(0)
            await service.cancel_run(record.run_id)
            for _ in range(20):
                if record.status == RunStatus.CANCELLED:
                    break
                await asyncio.sleep(0.01)

        assert record.status == RunStatus.CANCELLED
        assert record.finished_at is not None
        assert await manager.list() == []

    asyncio.run(scenario())


def test_run_service_lists_runs_as_viewmodels(tmp_path: Path) -> None:
    manager = RunManager()
    service = RunService(ConfigService(tmp_path / "config.toml"), manager)

    async def scenario() -> None:
        blocker = asyncio.Event()

        async def success(_: object) -> bool:
            return True

        async def running(_: object) -> bool:
            await blocker.wait()
            return True

        await manager.start(RunOrigin.MANUAL, "demo", "backup", "local", success, run_id="run-1")
        second = await manager.start(
            RunOrigin.MANUAL,
            "demo",
            "backup",
            "local",
            running,
            dry_run=True,
            run_id="run-2",
        )
        await asyncio.sleep(0)

        list_view = await service.list_runs_view(page_size=10)
        detail = await service.get_run_view("run-2")
        fragment = await service.get_run_status_view(
            "run-2", action_job="demo", action_step="backup.local", dry_run=True
        )

        assert list_view["active_count"] == 1
        assert [run["run_id"] for run in list_view["runs"]] == ["run-2", "run-1"]
        assert detail["task_label"] == "Backup local"
        assert detail["task_substep_label"] is None
        assert detail["target_primary"] == "demo: local"
        assert detail["target_secondary"] is None
        assert detail["status_label"] == "Running"
        assert detail["origin"] == "manual"
        assert detail["origin_label"] == "Manual"
        assert detail["is_cancellable"] is True
        assert detail["cancel_url"] == "/runs/run-2/cancel"
        assert detail["log_stream_url"] == "/diagnostics/logs/demo/stream"
        assert fragment["action_job"] == "demo"
        assert fragment["dry_run"] is True

        await manager.cancel(second.run_id)
        await asyncio.sleep(0)

    asyncio.run(scenario())


def test_run_status_view_without_actions_blanks_run_trigger(tmp_path: Path) -> None:
    manager = RunManager()
    service = RunService(ConfigService(tmp_path / "config.toml"), manager)

    async def scenario() -> None:
        blocker = asyncio.Event()

        async def success(_: object) -> bool:
            await blocker.wait()
            return True

        await manager.start(RunOrigin.MANUAL, "demo", "backup", "local", success, run_id="run-1")
        await asyncio.sleep(0)

        with_actions = await service.get_run_status_view("run-1")
        without_actions = await service.get_run_status_view("run-1", with_actions=False)

        assert with_actions["action_target"] is not None
        assert with_actions["action_job"] == "demo"
        assert with_actions["log_stream_url"] == "/diagnostics/logs/demo/stream"
        assert without_actions["action_target"] is None
        assert without_actions["action_job"] == ""
        assert without_actions["action_step"] == ""

        blocker.set()
        await _wait_for_terminal(manager, "run-1")

    asyncio.run(scenario())


def test_active_run_job_names_reports_running_jobs(tmp_path: Path) -> None:
    manager = RunManager()
    service = RunService(ConfigService(tmp_path / "config.toml"), manager)

    async def scenario() -> None:
        blocker = asyncio.Event()

        async def running(_: object) -> bool:
            await blocker.wait()
            return True

        async def success(_: object) -> bool:
            return True

        await manager.start(RunOrigin.MANUAL, "alpha", "backup", "local", running, run_id="r-a")
        await manager.start(RunOrigin.MANUAL, "beta", "backup", "local", success, run_id="r-b")
        await asyncio.sleep(0)
        await _wait_for_terminal(manager, "r-b")

        active = await service.active_run_job_names()
        assert "alpha" in active
        assert "beta" not in active

        blocker.set()
        await _wait_for_terminal(manager, "r-a")

    asyncio.run(scenario())


def test_active_run_job_names_reports_restore_job(tmp_path: Path) -> None:
    manager = RunManager()
    service = RunService(ConfigService(tmp_path / "config.toml"), manager)

    async def scenario() -> None:
        blocker = asyncio.Event()

        async def running(_: object) -> bool:
            await blocker.wait()
            return True

        await manager.start(
            RunOrigin.MANUAL,
            "demo",
            "restore",
            "local",
            running,
            run_kind=RunKind.RESTORE,
            run_id="restore-1",
        )
        await asyncio.sleep(0)

        active = await service.active_run_job_names()
        assert active == {"demo"}

        blocker.set()
        await _wait_for_terminal(manager, "restore-1")

    asyncio.run(scenario())


def test_active_run_job_names_keeps_local_jobs_when_scheduler_unreachable(
    tmp_path: Path,
) -> None:
    manager = RunManager()
    client = _FakeRunControlClient(
        list_error=ServiceError("scheduler_unreachable", "down", status_code=503)
    )
    service = RunService(
        ConfigService(tmp_path / "config.toml"), manager, run_control_client=client
    )

    async def scenario() -> None:
        blocker = asyncio.Event()

        async def running(_: object) -> bool:
            await blocker.wait()
            return True

        await manager.start(RunOrigin.MANUAL, "alpha", "backup", "local", running, run_id="r-a")
        await asyncio.sleep(0)

        active = await service.active_run_job_names()
        assert active == {"alpha"}

        blocker.set()
        await _wait_for_terminal(manager, "r-a")

    asyncio.run(scenario())


def test_run_service_renders_restore_runs_as_restore_kind(tmp_path: Path) -> None:
    manager = RunManager()
    service = RunService(ConfigService(tmp_path / "config.toml"), manager)

    async def scenario() -> None:
        async def success(_: object) -> bool:
            return True

        await manager.start(
            RunOrigin.MANUAL,
            "demo",
            "restore",
            "local",
            success,
            run_kind=RunKind.RESTORE,
            run_id="restore-1",
        )
        await manager.start(RunOrigin.MANUAL, "demo", "backup", "local", success, run_id="run-1")
        await asyncio.sleep(0)

        list_view = await service.list_runs_view(page_size=10)
        detail = await service.get_run_view("restore-1")

        restore_run = next(run for run in list_view["runs"] if run["run_id"] == "restore-1")
        assert restore_run["task_kind"] == "restore"
        assert restore_run["task_type_label"] == "Restore"
        assert restore_run["task_name"] == "demo.local"
        assert restore_run["target_primary"] == "demo: local"
        assert restore_run["target_secondary"] is None
        assert restore_run["job"] == "demo"
        assert restore_run["task_label"] == "Restore demo.local"
        assert restore_run["target"] == "demo.restore.local"
        assert restore_run["detail_url"] == "/runs/restore-1"
        assert restore_run["logs_url"] is None
        assert restore_run["log_stream_url"] is None

        assert detail["task_kind"] == "restore"
        assert detail["detail_url"] == "/runs/restore-1"
        assert detail["logs_url"] is None
        assert detail["log_stream_url"] is None

    asyncio.run(scenario())


def test_run_service_detail_view_separates_steps_and_restore_details(tmp_path: Path) -> None:
    history = RunHistoryService(tmp_path / "appdata.db")
    service = RunService(
        ConfigService(tmp_path / "config.toml"),
        RunManager(),
        run_history_service=history,
    )

    with closing(connect_appdata_db(history.db_path)) as conn:
        conn.execute("""
            INSERT INTO runs (
                run_id, origin, run_kind, job, task_type, task_name,
                started_at, finished_at, status, error, dry_run
            )
            VALUES
                (
                    'job-run', 'manual', 'job_task', 'demo', 'backup', 'local',
                    '2026-06-28T10:00:00Z', '2026-06-28T10:01:00Z',
                    'success', NULL, 0
                ),
                (
                    'restore-run', 'manual', 'restore', 'demo', 'restore', 'local',
                    '2026-06-28T11:00:00Z', '2026-06-28T11:01:00Z',
                    'success', NULL, 0
                )
            """)
        conn.execute("""
            INSERT INTO run_steps (
                run_step_id, run_id, position, step, backend, task_type,
                task_name, started_at, finished_at, status, error,
                effective_task_config_json
            )
            VALUES (
                'step-1', 'job-run', 0, 'backup', 'restic', 'backup',
                'local', '2026-06-28T10:00:00Z', '2026-06-28T10:01:00Z',
                'success', NULL, '{"repository":"/repo"}'
            ),
            (
                'step-2', 'job-run', 1, 'retention', 'restic', 'backup',
                'local', '2026-06-28T10:01:00Z', '2026-06-28T10:01:30Z',
                'success', NULL, '{"retention":{"keep_last":3},"repository":"/repo"}'
            )
            """)
        conn.execute("""
            INSERT INTO run_restores (
                run_restore_id, run_id, job, backup, backend, snapshot_id, mode,
                restore_target, snapshot_paths_json, include_patterns_json,
                exclude_patterns_json, overwrite, error, output, output_truncated
            )
            VALUES (
                'restore-detail-1', 'restore-run', 'demo', 'local', 'restic',
                'abcdef1234567890', 'browser', '/restore/demo/local',
                '["/data"]', '[]', '[]', 0, NULL, 'restored data', 0
            )
            """)

    async def scenario() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        return (
            await service.get_run_view("job-run"),
            await service.get_run_view("restore-run"),
            await service.list_runs_view(page_size=10),
        )

    job_view, restore_view, list_view = asyncio.run(scenario())

    assert job_view["has_steps"] is True
    assert job_view["has_restore"] is False
    assert [step["step"] for step in job_view["steps"]] == ["backup", "retention"]
    assert job_view["steps"][0]["effective_task_config_pretty"] == '{\n  "repository": "/repo"\n}'
    assert job_view["steps"][1]["effective_task_config_pretty"] == (
        '{\n  "repository": "/repo",\n  "retention": {\n    "keep_last": 3\n  }\n}'
    )
    assert job_view["restore"] is None

    assert restore_view["has_steps"] is False
    assert restore_view["has_restore"] is True
    assert restore_view["steps"] == []
    restore_detail = restore_view["restore"]
    assert isinstance(restore_detail, dict)
    assert restore_detail["run_id"] == "restore-run"
    assert restore_detail["run_restore_id"] == "restore-detail-1"
    assert restore_detail["snapshot_paths"] == ["/data"]
    assert restore_view["task_label"] == "Restore demo.local @ abcdef12"
    listed_restore = next(run for run in list_view["runs"] if run["run_id"] == "restore-run")
    assert listed_restore["task_label"] == "Restore demo.local"
    assert listed_restore["detail_url"] == "/runs/restore-run"


def test_run_service_detail_view_shows_steps_for_a_still_running_run(tmp_path: Path) -> None:
    history = RunHistoryService(tmp_path / "appdata.db")
    run_manager = RunManager()
    service = RunService(
        ConfigService(tmp_path / "config.toml"),
        run_manager,
        run_history_service=history,
    )

    with closing(connect_appdata_db(history.db_path)) as conn:
        conn.execute("""
            INSERT INTO runs (
                run_id, origin, run_kind, job, task_type, task_name,
                started_at, status, dry_run
            )
            VALUES (
                'live-run', 'manual', 'job_task', 'demo', 'backup', 'local',
                '2026-06-28T10:00:00Z', 'running', 0
            )
            """)
        conn.execute("""
            INSERT INTO run_steps (
                run_step_id, run_id, position, step, backend, task_type, task_name,
                started_at, status, effective_task_config_json
            )
            VALUES (
                'live-step-1', 'live-run', 0, 'backup', 'restic', 'backup', 'local',
                '2026-06-28T10:00:00Z', 'success', '{}'
            )
            """)

    async def scenario() -> dict[str, object]:
        started = asyncio.Event()
        finish = asyncio.Event()

        async def operation(_mark_not_cancellable: object) -> bool:
            started.set()
            await finish.wait()
            return True

        await run_manager.start(
            RunOrigin.MANUAL, "demo", "backup", "local", operation, run_id="live-run"
        )
        await started.wait()
        view = await service.get_run_view("live-run")
        finish.set()
        await run_manager.join("live-run")
        return view

    view = asyncio.run(scenario())

    assert view["status"] == "running"
    assert view["has_steps"] is True
    assert [step["step"] for step in view["steps"]] == ["backup"]
    assert view["has_restore"] is False


def test_run_service_detail_view_links_to_repository_context_while_restore_is_running(
    tmp_path: Path,
) -> None:
    history = RunHistoryService(tmp_path / "appdata.db")
    run_manager = RunManager()
    service = RunService(
        ConfigService(tmp_path / "config.toml"),
        run_manager,
        run_history_service=history,
    )

    with closing(connect_appdata_db(history.db_path)) as conn:
        conn.execute("""
            INSERT INTO runs (
                run_id, origin, run_kind, job, task_type, task_name,
                started_at, status, dry_run
            )
            VALUES (
                'live-restore', 'manual', 'restore', 'demo', 'restore', 'local',
                '2026-06-28T10:00:00Z', 'running', 0
            )
            """)
        conn.execute("""
            INSERT INTO run_restores (
                run_restore_id, run_id, job, backup, backend, snapshot_id, mode,
                restore_target, snapshot_paths_json, include_patterns_json,
                exclude_patterns_json, overwrite, output_truncated
            )
            VALUES (
                'live-restore-detail', 'live-restore', 'demo', 'local', 'restic',
                'abcdef1234567890', 'pattern', '/restore/demo/local', '[]', '[]', '[]', 0, 0
            )
            """)

    async def scenario() -> dict[str, object]:
        started = asyncio.Event()
        finish = asyncio.Event()

        async def operation(_mark_not_cancellable: object) -> bool:
            started.set()
            await finish.wait()
            return True

        await run_manager.start(
            RunOrigin.MANUAL,
            "demo",
            "restore",
            "local",
            operation,
            run_kind=RunKind.RESTORE,
            run_id="live-restore",
        )
        await started.wait()
        view = await service.get_run_view("live-restore")
        finish.set()
        await run_manager.join("live-restore")
        return view

    view = asyncio.run(scenario())

    assert view["status"] == "running"
    assert view["detail_url"] == "/runs/live-restore"
    assert view["task_label"] == "Restore demo.local @ abcdef12"


class _FakeRunControlClient:
    """In-memory stand-in for RunControlClient used in RunService tests."""

    def __init__(
        self,
        runs: list[dict[str, object]] | None = None,
        *,
        list_error: ServiceError | None = None,
        cancel_error: ServiceError | None = None,
    ) -> None:
        self._runs = runs or []
        self._list_error = list_error
        self._cancel_error = cancel_error
        self.cancelled: list[str] = []

    async def list_runs(self) -> list[dict[str, object]]:
        if self._list_error is not None:
            raise self._list_error
        return self._runs

    async def cancel_run(self, run_id: str) -> dict[str, object]:
        if self._cancel_error is not None:
            raise self._cancel_error
        self.cancelled.append(run_id)
        return {
            "run_id": run_id,
            "origin": "scheduler",
            "run_kind": "job_task",
            "job": "demo",
            "task_type": "backup",
            "task_name": "local",
            "target": "demo.backup.local",
            "status": "cancelled",
            "dry_run": False,
            "cancellable": False,
            "created_at": None,
            "started_at": None,
            "finished_at": "2026-05-27T12:00:05+00:00",
            "error": None,
        }


def _scheduler_run_payload(
    run_id: str,
    *,
    status: str = "running",
    task_type: str = "workflow",
    task_name: str = "nightly",
    origin: str = "scheduler",
    cancellable: bool = True,
    started_at: str | None = "2026-05-27T11:00:01+00:00",
    finished_at: str | None = None,
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "origin": origin,
        "run_kind": "job_task",
        "job": "demo",
        "task_type": task_type,
        "task_name": task_name,
        "target": f"demo.{task_type}.{task_name}",
        "status": status,
        "dry_run": False,
        "cancellable": cancellable,
        "created_at": "2026-05-27T11:00:00+00:00",
        "started_at": started_at,
        "finished_at": finished_at,
        "error": None,
    }


def _history_record(
    run_id: str,
    *,
    job: str = "demo",
    task_type: str = "backup",
    task_name: str = "local",
    run_kind: RunKind = RunKind.JOB_TASK,
    status: RunStatus = RunStatus.SUCCESS,
    finished_offset: int = 0,
) -> RunRecord:
    created_at = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc) + timedelta(
        minutes=finished_offset
    )
    return RunRecord(
        run_id=run_id,
        origin=RunOrigin.SCHEDULER,
        run_kind=run_kind,
        job=job,
        task_type=task_type,
        task_name=task_name,
        status=status,
        dry_run=False,
        cancellable=False,
        created_at=created_at,
        started_at=created_at + timedelta(seconds=1),
        finished_at=created_at + timedelta(seconds=5),
        error=None,
    )


def test_run_service_merges_manual_and_scheduler_runs(tmp_path: Path) -> None:
    manager = RunManager()
    scheduler_runs = [_scheduler_run_payload("sched-1")]
    client = _FakeRunControlClient(scheduler_runs)
    service = RunService(
        ConfigService(tmp_path / "config.toml"), manager, run_control_client=client
    )

    async def scenario() -> None:
        async def success(_: object) -> bool:
            return True

        await manager.start(RunOrigin.MANUAL, "demo", "backup", "local", success, run_id="manual-1")
        await asyncio.sleep(0)

        view = await service.list_runs_view(page_size=10)

        assert view["scheduler_available"] is True
        run_ids = {run["run_id"] for run in view["runs"]}  # type: ignore[union-attr]
        assert run_ids == {"manual-1", "sched-1"}
        scheduler_view = next(
            run for run in view["runs"] if run["run_id"] == "sched-1"  # type: ignore[index]
        )
        assert scheduler_view["origin"] == "scheduler"
        assert scheduler_view["is_active"] is True
        assert scheduler_view["is_cancellable"] is True
        assert scheduler_view["cancel_url"] == "/runs/sched-1/cancel"
        assert view["active_count"] == 1

    asyncio.run(scenario())


def test_run_service_active_runs_view_returns_only_active_with_cancel_fields(
    tmp_path: Path,
) -> None:
    manager = RunManager()
    client = _FakeRunControlClient([_scheduler_run_payload("sched-1")])
    service = RunService(
        ConfigService(tmp_path / "config.toml"), manager, run_control_client=client
    )

    async def scenario() -> None:
        async def success(_: object) -> bool:
            return True

        await manager.start(
            RunOrigin.MANUAL, "demo", "backup", "local", success, run_id="manual-terminal"
        )
        await asyncio.sleep(0)

        view = await service.list_active_runs_view()

        assert view["scheduler_available"] is True
        assert [run["run_id"] for run in view["runs"]] == ["sched-1"]
        assert view["runs"][0]["is_cancellable"] is True
        assert view["runs"][0]["cancel_url"] == "/runs/sched-1/cancel"
        assert view["runs"][0]["status_url"] == "/runs/sched-1/status"

    asyncio.run(scenario())


def test_run_service_merges_history_deduplicates_live_runs_and_applies_limit(
    tmp_path: Path,
) -> None:
    manager = RunManager()
    history = RunHistoryService(tmp_path / "appdata.db")
    scheduler_runs = [
        _scheduler_run_payload(
            "sched-live",
            started_at="2026-05-27T12:04:01+00:00",
        )
    ]
    client = _FakeRunControlClient(scheduler_runs)
    service = RunService(
        ConfigService(tmp_path / "config.toml"),
        manager,
        run_control_client=client,
        run_history_service=history,
    )

    async def scenario() -> None:
        async def success(_: object) -> bool:
            return True

        await history.record(_history_record("hist-old", finished_offset=1))
        await history.record(_history_record("sched-live", finished_offset=2))
        await history.record(_history_record("hist-new", finished_offset=3))
        await manager.start(
            RunOrigin.MANUAL,
            "demo",
            "backup",
            "local",
            success,
            run_id="manual-live",
        )
        await asyncio.sleep(0)

        view = await service.list_runs_view(page_size=2)

        assert [run["run_id"] for run in view["runs"]] == [  # type: ignore[union-attr]
            "manual-live",
            "sched-live",
            "hist-new",
            "hist-old",
        ]
        assert view["total_count"] == 4
        assert view["active_count"] == 1
        assert view["has_next_page"] is False

    asyncio.run(scenario())


def test_run_service_paginates_history_after_first_page_live_runs(
    tmp_path: Path,
) -> None:
    manager = RunManager()
    history = RunHistoryService(tmp_path / "appdata.db")
    service = RunService(
        ConfigService(tmp_path / "config.toml"),
        manager,
        run_history_service=history,
    )

    async def scenario() -> None:
        blocker = asyncio.Event()

        async def running(_: object) -> bool:
            await blocker.wait()
            return True

        for index in range(5):
            await history.record(_history_record(f"hist-{index}", finished_offset=index))
        await manager.start(
            RunOrigin.MANUAL,
            "demo",
            "backup",
            "local",
            running,
            run_id="manual-live",
        )
        await asyncio.sleep(0)

        first_page = await service.list_runs_view(page=1, page_size=3)
        second_page = await service.list_runs_view(page=2, page_size=3)

        assert [run["run_id"] for run in first_page["runs"]] == [
            "manual-live",
            "hist-4",
            "hist-3",
            "hist-2",
        ]
        assert first_page["has_next_page"] is True
        assert first_page["has_previous_page"] is False
        assert first_page["next_page_url"] == "/runs?page=2"

        assert [run["run_id"] for run in second_page["runs"]] == [
            "hist-1",
            "hist-0",
        ]
        assert second_page["has_next_page"] is False
        assert second_page["has_previous_page"] is True
        assert second_page["previous_page_url"] == "/runs?page=1"

        blocker.set()
        await _wait_for_terminal(manager, "manual-live")

    asyncio.run(scenario())


def test_run_service_paginates_filtered_history_when_live_run_is_also_historical(
    tmp_path: Path,
) -> None:
    manager = RunManager()
    history = RunHistoryService(tmp_path / "appdata.db")
    scheduler_runs = [
        _scheduler_run_payload(
            "sched-live",
            started_at="2026-05-27T12:04:01+00:00",
        )
    ]
    client = _FakeRunControlClient(scheduler_runs)
    service = RunService(
        ConfigService(tmp_path / "config.toml"),
        manager,
        run_control_client=client,
        run_history_service=history,
    )

    async def scenario() -> None:
        for index in range(3):
            await history.record(_history_record(f"hist-{index}", finished_offset=index))
        await history.record(_history_record("sched-live", finished_offset=3))

        first_page = await service.list_runs_view(page=1, page_size=2)
        second_page = await service.list_runs_view(page=2, page_size=2)

        assert [run["run_id"] for run in first_page["runs"]] == [
            "sched-live",
            "hist-2",
            "hist-1",
        ]
        assert first_page["has_next_page"] is True
        assert [run["run_id"] for run in second_page["runs"]] == ["hist-0"]
        assert second_page["has_next_page"] is False

    asyncio.run(scenario())


def test_run_service_filters_history_but_keeps_active_runs_visible(tmp_path: Path) -> None:
    manager = RunManager()
    history = RunHistoryService(tmp_path / "appdata.db")
    service = RunService(
        ConfigService(tmp_path / "config.toml"),
        manager,
        run_history_service=history,
    )

    async def scenario() -> None:
        blocker = asyncio.Event()

        async def running(_: object) -> bool:
            await blocker.wait()
            return True

        await history.record(
            _history_record(
                "failed-demo",
                job="demo",
                status=RunStatus.FAILED,
                finished_offset=2,
            )
        )
        await history.record(
            _history_record(
                "success-demo",
                job="demo",
                status=RunStatus.SUCCESS,
                finished_offset=1,
            )
        )
        await history.record(
            _history_record(
                "failed-other",
                job="other",
                status=RunStatus.FAILED,
                finished_offset=3,
            )
        )
        await manager.start(
            RunOrigin.MANUAL,
            "other",
            "backup",
            "local",
            running,
            run_id="manual-live",
        )
        await asyncio.sleep(0)

        view = await service.list_runs_view(
            page=1,
            page_size=1,
            job="demo",
            task="backup.local",
            status="failed",
            origin="scheduler",
        )

        assert [run["run_id"] for run in view["active_runs"]] == ["manual-live"]
        assert [run["run_id"] for run in view["history_runs"]] == ["failed-demo"]
        assert [run["run_id"] for run in view["runs"]] == ["manual-live", "failed-demo"]
        assert view["filters"] == {
            "job": "demo",
            "task": "backup.local",
            "status": "failed",
            "origin": "scheduler",
            "is_active": True,
        }
        assert view["has_next_page"] is False
        assert view["filter_clear_url"] == "/runs"
        assert {"value": "demo", "label": "demo"} in view["filter_options"]["jobs"]
        assert {
            "value": "backup.local",
            "label": "Backup local",
        } in view[
            "filter_options"
        ]["tasks"]
        assert {"value": "failed", "label": "Failed"} in view["filter_options"]["statuses"]
        assert {
            "value": "scheduler",
            "label": "Scheduler",
        } in view[
            "filter_options"
        ]["origins"]

        blocker.set()
        await _wait_for_terminal(manager, "manual-live")

    asyncio.run(scenario())


def test_run_service_does_not_treat_historical_running_rows_as_active(
    tmp_path: Path,
) -> None:
    history = RunHistoryService(tmp_path / "appdata.db")
    service = RunService(
        ConfigService(tmp_path / "config.toml"),
        RunManager(),
        run_history_service=history,
    )

    async def scenario() -> None:
        await history.create_run(_history_record("orphaned", status=RunStatus.RUNNING))

        view = await service.list_runs_view(page_size=10)

        assert view["active_count"] == 0
        assert len(view["runs"]) == 1
        run = view["runs"][0]  # type: ignore[index]
        assert run["run_id"] == "orphaned"
        assert run["status"] == RunStatus.RUNNING
        assert run["is_active"] is False
        assert run["is_cancellable"] is False
        assert run["log_stream_url"] is None

    asyncio.run(scenario())


def test_run_service_marks_scheduler_unavailable_when_unreachable(tmp_path: Path) -> None:
    manager = RunManager()
    client = _FakeRunControlClient(
        list_error=ServiceError("scheduler_unreachable", "down", status_code=503)
    )
    service = RunService(
        ConfigService(tmp_path / "config.toml"), manager, run_control_client=client
    )

    async def scenario() -> None:
        async def success(_: object) -> bool:
            return True

        await manager.start(RunOrigin.MANUAL, "demo", "backup", "local", success, run_id="manual-1")
        await asyncio.sleep(0)

        view = await service.list_runs_view(page_size=10)

        assert view["scheduler_available"] is False
        assert [run["run_id"] for run in view["runs"]] == ["manual-1"]  # type: ignore[union-attr]

    asyncio.run(scenario())


def test_run_service_cancel_routes_local_run_to_manager(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_repo_config(config_path)
    manager = RunManager()
    client = _FakeRunControlClient()
    service = RunService(
        ConfigService(config_path),
        manager,
        tmp_path / "locks",
        tmp_path / "logs",
        run_control_client=client,
    )

    async def wait_forever(*_args: object) -> bool:
        await asyncio.Event().wait()
        return True

    async def scenario() -> None:
        with patch("src.services.runs._run_parsed_task_selector", side_effect=wait_forever):
            record = await service.start_run("demo.backup.local")
            await asyncio.sleep(0)
            result = await service.cancel_run(record.run_id)
            for _ in range(20):
                if record.status == RunStatus.CANCELLED:
                    break
                await asyncio.sleep(0.01)

        assert not isinstance(result, dict)
        assert client.cancelled == []
        assert record.status == RunStatus.CANCELLED

    asyncio.run(scenario())


def test_run_service_cancel_routes_unknown_run_to_scheduler(tmp_path: Path) -> None:
    manager = RunManager()
    client = _FakeRunControlClient()
    service = RunService(
        ConfigService(tmp_path / "config.toml"), manager, run_control_client=client
    )

    async def scenario() -> None:
        result = await service.cancel_run("sched-9")
        assert isinstance(result, dict)
        assert result["run_id"] == "sched-9"
        assert client.cancelled == ["sched-9"]

    asyncio.run(scenario())


def test_run_service_cancel_reraises_not_found_without_client(tmp_path: Path) -> None:
    manager = RunManager()
    service = RunService(ConfigService(tmp_path / "config.toml"), manager)

    async def scenario() -> None:
        with pytest.raises(NotFoundServiceError):
            await service.cancel_run("missing")

    asyncio.run(scenario())


def test_run_service_cancel_propagates_scheduler_not_found(tmp_path: Path) -> None:
    manager = RunManager()
    client = _FakeRunControlClient(
        cancel_error=NotFoundServiceError("Run not found: x", code="run_not_found")
    )
    service = RunService(
        ConfigService(tmp_path / "config.toml"), manager, run_control_client=client
    )

    async def scenario() -> None:
        with pytest.raises(NotFoundServiceError):
            await service.cancel_run("missing")

    asyncio.run(scenario())


def test_run_service_scheduler_status_view_has_no_run_actions(tmp_path: Path) -> None:
    service = RunService(ConfigService(tmp_path / "config.toml"), RunManager())

    view = service.scheduler_status_view(
        {
            "run_id": "sched-1",
            "origin": "scheduler",
            "run_kind": "job_task",
            "job": "demo",
            "task_type": "workflow",
            "task_name": "nightly",
            "target": "demo.workflow.nightly",
            "status": "cancelled",
            "dry_run": False,
            "cancellable": False,
            "created_at": None,
            "started_at": None,
            "finished_at": "2026-05-27T12:00:05+00:00",
            "error": None,
        }
    )

    assert view["status"] == "cancelled"
    assert view["status_label"] == "Cancelled"
    assert view["is_cancellable"] is False
    assert view["action_job"] == ""
    assert view["action_step"] == ""
    assert view["action_target"] is None
    assert view["cancel_url"] == "/runs/sched-1/cancel"


def test_run_service_runtime_stopping_status_is_warning(tmp_path: Path) -> None:
    service = RunService(ConfigService(tmp_path / "config.toml"), RunManager())

    view = service.scheduler_status_view(
        {
            "run_id": "sched-stopping",
            "status": "runtime_stopping",
            "run_kind": "job_task",
            "job": "demo",
            "task_type": "backup",
            "task_name": "local",
            "target": "demo.backup.local",
        }
    )

    assert view["status_label"] == "Shutting down"
    assert view["status_tone"] == "amber"
    assert view["is_active"] is False


def test_run_service_start_status_view_builds_immediate_error_view(tmp_path: Path) -> None:
    service = RunService(ConfigService(tmp_path / "config.toml"), RunManager())

    async def scenario() -> None:
        with patch.object(
            service,
            "start_run",
            AsyncMock(side_effect=ServiceError("runtime_stopping", "Runtime is stopping", 503)),
        ):
            view = await service.start_run_status_view(
                "demo.backup.local",
                action_job="demo",
                action_step="backup.local",
                dry_run=True,
            )

        assert view["status"] == "runtime_stopping"
        assert view["status_label"] == "Shutting down"
        assert view["status_tone"] == "amber"
        assert view["error"] == "Runtime is stopping"
        assert view["dry_run"] is True
        assert view["action_target"] == "/jobs/demo/backup.local/dry-run"

    asyncio.run(scenario())


def test_run_service_start_status_view_labels_expected_service_errors(tmp_path: Path) -> None:
    service = RunService(ConfigService(tmp_path / "config.toml"), RunManager())

    async def scenario() -> None:
        with patch.object(
            service,
            "start_run",
            AsyncMock(
                side_effect=ServiceError(
                    "invalid_task_selector",
                    "Task selector must be JOB.backup.NAME",
                    400,
                )
            ),
        ):
            view = await service.start_run_status_view(
                "bad-target",
                action_job="demo",
                action_step="backup.local",
            )

        assert view["status"] == "invalid_task_selector"
        assert view["status_label"] == "Invalid target"
        assert view["status_tone"] == "red"
        assert view["error"] == "Task selector must be JOB.backup.NAME"

    asyncio.run(scenario())


def test_run_service_historical_active_run_is_shown_as_interrupted(tmp_path: Path) -> None:
    history = RunHistoryService(tmp_path / "appdata.db")
    service = RunService(
        ConfigService(tmp_path / "config.toml"),
        RunManager(),
        run_history_service=history,
    )
    record = RunRecord(
        run_id="manual-cli-stale",
        origin=RunOrigin.MANUAL,
        run_kind=RunKind.JOB_TASK,
        job="demo",
        task_type="backup",
        task_name="local",
        status=RunStatus.RUNNING,
        dry_run=False,
        started_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )

    async def scenario() -> dict[str, object]:
        await history.create_run(record)
        return await service.list_runs_view(page_size=10)

    view = asyncio.run(scenario())
    run = view["runs"][0]

    assert run["run_id"] == "manual-cli-stale"
    assert run["status"] == "running"
    assert run["status_label"] == "Interrupted"
    assert run["status_tone"] == "amber"
    assert run["is_active"] is False
    assert run["is_cancellable"] is False
    assert run["log_stream_url"] is None
    assert run["error"] == "Run was left active by a previous process."


def test_run_service_lists_only_manual_runs_without_client(tmp_path: Path) -> None:
    manager = RunManager()
    service = RunService(ConfigService(tmp_path / "config.toml"), manager)

    async def scenario() -> None:
        blocker = asyncio.Event()

        async def success(_: object) -> bool:
            await blocker.wait()
            return True

        await manager.start(RunOrigin.MANUAL, "demo", "backup", "local", success, run_id="manual-1")
        await asyncio.sleep(0)

        view = await service.list_runs_view(page_size=10)

        assert view["scheduler_available"] is False
        assert [run["run_id"] for run in view["runs"]] == ["manual-1"]  # type: ignore[union-attr]

        blocker.set()
        await _wait_for_terminal(manager, "manual-1")

    asyncio.run(scenario())


def test_run_service_records_lock_error(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_repo_config(config_path)
    manager = RunManager()
    service = RunService(ConfigService(config_path), manager, tmp_path / "locks", tmp_path / "logs")

    async def scenario() -> None:
        with patch(
            "src.services.runs._run_parsed_task_selector",
            side_effect=JobAlreadyRunningError("demo", "local", tmp_path / "demo.lock"),
        ):
            record = await service.start_run("demo.backup.local")
            result = await manager.join(record.run_id)

        assert result.status == RunStatus.LOCK_ERROR
        assert "already running" in str(result.error)

    asyncio.run(scenario())


def test_run_service_start_run_status_view_surfaces_lock_error_in_first_response(
    tmp_path: Path,
) -> None:
    """A fast resource-lock conflict is visible in the very first status-view
    response, not only after the caller polls again (DK-BUG-AUDIT-011)."""
    config_path = tmp_path / "config.toml"
    _write_repo_config(config_path)
    manager = RunManager()
    service = RunService(
        ConfigService(config_path),
        manager,
        tmp_path / "locks",
        tmp_path / "logs",
    )

    async def scenario() -> dict[str, object]:
        with patch(
            "src.services.runs._run_parsed_task_selector",
            side_effect=JobAlreadyRunningError("demo", "local", tmp_path / "demo.lock"),
        ):
            return await service.start_run_status_view(
                "demo.backup.local", action_job="demo", action_step="backup.local"
            )

    view = asyncio.run(scenario())

    assert view["status"] == RunStatus.LOCK_ERROR


def test_run_service_reuses_active_manual_run_for_same_target(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_repo_config(config_path)
    manager = RunManager()
    service = RunService(
        ConfigService(config_path),
        manager,
        tmp_path / "locks",
        tmp_path / "logs",
    )

    async def scenario() -> None:
        release = asyncio.Event()

        async def running(_: object, __: object) -> bool:
            await release.wait()
            return True

        with patch("src.services.runs._run_parsed_task_selector", side_effect=running):
            first = await service.start_run("demo.backup.local")
            second = await service.start_run("demo.backup.local")

            assert second.run_id == first.run_id
            assert [run.run_id for run in await manager.list()] == [first.run_id]

            release.set()
            await _wait_for_terminal(manager, first.run_id)

    asyncio.run(scenario())


def test_run_service_does_not_reuse_active_run_with_different_dry_run(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_repo_config(config_path)
    manager = RunManager()
    service = RunService(
        ConfigService(config_path),
        manager,
        tmp_path / "locks",
        tmp_path / "logs",
    )

    async def scenario() -> None:
        release = asyncio.Event()

        async def running(_: object, __: object) -> bool:
            await release.wait()
            return True

        with patch("src.services.runs._run_parsed_task_selector", side_effect=running):
            real = await service.start_run("demo.backup.local")
            dry = await service.start_run("demo.backup.local", dry_run=True)

            assert dry.run_id != real.run_id
            assert len(await manager.list()) == 2

            release.set()
            await _wait_for_terminal(manager, real.run_id)
            await _wait_for_terminal(manager, dry.run_id)

    asyncio.run(scenario())


def _write_repo_config_with_retention_and_hooks(path: Path) -> None:
    path.write_text(
        """
[jobs.demo.backup]
password = "job-secret"

[jobs.demo.backup.local]
repository = "/repo/secret-value"
sources = ["/data"]
retention = true
cleanup = true
keep_daily = 7
pre_hooks = ["/scripts/pre.sh"]
""".strip(),
        encoding="utf-8",
    )


def test_run_service_backup_persists_canonical_run_steps_with_effective_config(
    tmp_path: Path,
) -> None:
    """Ein Backup-Run erzeugt `runs` + `run_steps` mit kanonischen Step-Suffixen.

    `run_steps.effective_task_config_json` enthaelt den fuer den jeweiligen Step
    wirksamen, aufgeloesten Config-Ausschnitt, aber ohne persistierte Secrets:
    eigene Hooks bleiben sichtbar, ein direktes Passwort wird maskiert.
    """
    config_path = tmp_path / "config.toml"
    _write_repo_config_with_retention_and_hooks(config_path)
    history = RunHistoryService(tmp_path / "appdata.db")
    manager = RunManager(
        on_started=history.create_run,
        on_terminal=terminal_run_hook(history),
    )
    service = RunService(
        ConfigService(config_path),
        manager,
        tmp_path / "locks",
        tmp_path / "logs",
        run_history_service=history,
    )

    async def scenario() -> None:
        with (
            patch("src.core.workflow.BackupExecutor") as backup_mock,
            patch("src.core.workflow.ForgetExecutor") as forget_mock,
            patch("src.core.workflow.PruneExecutor") as prune_mock,
        ):
            backup_mock.return_value.execute = AsyncMock(return_value=True)
            forget_mock.return_value.execute = AsyncMock(return_value=True)
            prune_mock.return_value.execute = AsyncMock(return_value=True)
            record = await service.start_run("demo.backup.local", dry_run=True)
            await manager.join(record.run_id)

        with closing(history._connect()) as conn:
            run_row = conn.execute(
                "SELECT run_kind, job, task_type, task_name, status FROM runs WHERE run_id = ?",
                (record.run_id,),
            ).fetchone()
            step_rows = conn.execute(
                """
                SELECT step, status, effective_task_config_json FROM run_steps
                WHERE run_id = ? ORDER BY position
                """,
                (record.run_id,),
            ).fetchall()

        assert run_row["run_kind"] == "job_task"
        assert (run_row["job"], run_row["task_type"], run_row["task_name"]) == (
            "demo",
            "backup",
            "local",
        )
        assert run_row["status"] == "success"

        assert [row["step"] for row in step_rows] == [
            "backup.local.backup",
            "backup.local.retention",
            "backup.local.cleanup",
        ]
        assert all(
            row["step"].rsplit(".", 1)[-1] in {"backup", "retention", "cleanup", "rclone"}
            for row in step_rows
        )
        assert [row["status"] for row in step_rows] == ["success", "success", "success"]

        backup_step_config = json.loads(step_rows[0]["effective_task_config_json"])
        assert backup_step_config["hooks"]["pre_hooks"] == ["/scripts/pre.sh"]
        assert backup_step_config["credentials"]["password"] == "******"
        assert backup_step_config["execution"]["retention"] is True
        assert backup_step_config["execution"]["cleanup"] is True
        assert "job-secret" not in step_rows[0]["effective_task_config_json"]

    asyncio.run(scenario())


def test_run_service_lock_conflict_persists_run_without_run_steps(tmp_path: Path) -> None:
    """Ein Lock-Konflikt erzeugt nur eine `runs`-Zeile, keine `run_steps`."""
    config_path = tmp_path / "config.toml"
    _write_repo_config(config_path)
    lock_dir = tmp_path / "locks"
    history = RunHistoryService(tmp_path / "appdata.db")
    manager = RunManager(
        on_started=history.create_run,
        on_terminal=terminal_run_hook(history),
    )
    service = RunService(
        ConfigService(config_path),
        manager,
        lock_dir,
        tmp_path / "logs",
        run_history_service=history,
    )
    probe = ResourceLockManager(
        "other", "local", {resource_for_repository("/repo/secret-value")}, lock_dir=lock_dir
    )

    async def scenario() -> None:
        with probe.acquire():
            record = await service.start_run("demo.backup.local", dry_run=True)
            await manager.join(record.run_id)

        with closing(history._connect()) as conn:
            run_row = conn.execute(
                "SELECT status FROM runs WHERE run_id = ?", (record.run_id,)
            ).fetchone()
            step_count = conn.execute(
                "SELECT COUNT(*) AS n FROM run_steps WHERE run_id = ?", (record.run_id,)
            ).fetchone()["n"]

        assert run_row["status"] == "lock_error"
        assert step_count == 0

    asyncio.run(scenario())


class _RecordingStatsCollector:
    """Captures ``BackupRunStatsContext`` calls without touching any backend."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, BackupRunStatsContext]] = []

    async def collect_and_store_async(
        self, job: str, backup: str, context: BackupRunStatsContext
    ) -> None:
        self.calls.append((job, backup, context))


def test_run_service_rclone_step_persists_canonical_run_step_suffix(tmp_path: Path) -> None:
    """``run_steps.step`` fuer einen Rclone-Task endet kanonisch auf ``rclone``.

    Regressionstest: ``WorkflowEngine._run_rclone()`` schrieb bisher
    ``step=f"rclone.{name}"``, dessen letzter Punkt-Bestandteil der Task-Name
    war statt des kanonischen Typs ``rclone``.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[jobs.demo.rclone.offsite]
source = "/repo"
target = "remote:bucket"
""".strip(),
        encoding="utf-8",
    )
    history = RunHistoryService(tmp_path / "appdata.db")
    manager = RunManager(
        on_started=history.create_run,
        on_terminal=terminal_run_hook(history),
    )
    service = RunService(
        ConfigService(config_path),
        manager,
        tmp_path / "locks",
        tmp_path / "logs",
        run_history_service=history,
    )

    async def scenario() -> None:
        with patch("src.core.workflow.RcloneExecutor") as rclone_mock:
            rclone_mock.return_value.execute = AsyncMock(return_value=True)
            record = await service.start_run("demo.rclone.offsite", dry_run=True)
            await manager.join(record.run_id)

        with closing(history._connect()) as conn:
            steps = conn.execute(
                "SELECT step FROM run_steps WHERE run_id = ?", (record.run_id,)
            ).fetchall()

        assert [row["step"] for row in steps] == ["rclone.offsite.rclone"]
        assert all(
            row["step"].rsplit(".", 1)[-1] in {"backup", "retention", "cleanup", "rclone"}
            for row in steps
        )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("step", "trigger_kind", "executor_name"),
    [
        ("backup.local.retention", "retention", "ForgetExecutor"),
        ("backup.local.cleanup", "cleanup", "PruneExecutor"),
    ],
)
def test_run_service_individual_step_links_run_step_id_to_stats_context(
    tmp_path: Path, step: str, trigger_kind: str, executor_name: str
) -> None:
    """Ein einzeln ausgefuehrter Retention-/Cleanup-Step verlinkt seine eigene run_step_id.

    Regressionstest: ``JobRunner._stats_context_for_step()`` setzte in keinem
    Zweig ``run_step_id`` auf dem ``BackupRunStatsContext`` — nur der separate
    ``run_backup()``-Pfad tat dies. Dadurch blieben ``created_run_step_id``/
    ``removed_by_run_step_id`` fuer einzeln ausgefuehrte Steps unverlinkt.
    """
    config_path = tmp_path / "config.toml"
    _write_repo_config_with_retention_and_hooks(config_path)
    history = RunHistoryService(tmp_path / "appdata.db")
    manager = RunManager(
        on_started=history.create_run,
        on_terminal=terminal_run_hook(history),
    )
    stats = _RecordingStatsCollector()
    service = RunService(
        ConfigService(config_path),
        manager,
        tmp_path / "locks",
        tmp_path / "logs",
        stats_collector=stats,
        run_history_service=history,
    )

    async def scenario() -> None:
        with patch(f"src.core.workflow.{executor_name}") as executor_mock:
            executor_mock.return_value.execute = AsyncMock(return_value=True)
            record = await service.start_run(f"demo.{step}", dry_run=False)
            await manager.join(record.run_id)

        with closing(history._connect()) as conn:
            run_step_id = conn.execute(
                "SELECT run_step_id FROM run_steps WHERE run_id = ? AND step = ?",
                (record.run_id, step),
            ).fetchone()["run_step_id"]

        assert run_step_id is not None
        assert stats.calls == [
            (
                "demo",
                "local",
                BackupRunStatsContext(
                    run_id=record.run_id,
                    run_step_id=run_step_id,
                    trigger_kind=trigger_kind,
                ),
            )
        ]

    asyncio.run(scenario())


def test_run_service_workflow_steps_link_distinct_run_step_ids_to_stats_context(
    tmp_path: Path,
) -> None:
    """Jeder fachliche Workflow-Step verlinkt seine eigene run_step_id.

    Regressionstest fuer denselben Bug wie bei Einzel-Steps: ein Workflow mit
    Backup- und Retention-Step muss zwei unterschiedliche ``run_step_id``-Werte
    an den Stats-Collector melden, nicht zweimal dieselbe (oder ``None``).
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[jobs.demo.backup.local]
repository = "/repo/secret-value"
sources = ["/data"]
password = "secret-value"
keep_last = 3

[jobs.demo.workflow.nightly]
steps = ["backup.local.backup", "backup.local.retention"]
""".strip(),
        encoding="utf-8",
    )
    history = RunHistoryService(tmp_path / "appdata.db")
    manager = RunManager(
        on_started=history.create_run,
        on_terminal=terminal_run_hook(history),
    )
    stats = _RecordingStatsCollector()
    service = RunService(
        ConfigService(config_path),
        manager,
        tmp_path / "locks",
        tmp_path / "logs",
        stats_collector=stats,
        run_history_service=history,
    )

    async def scenario() -> None:
        with (
            patch("src.core.workflow.BackupExecutor") as backup_mock,
            patch("src.core.workflow.ForgetExecutor") as forget_mock,
        ):
            backup_mock.return_value.execute = AsyncMock(return_value=True)
            forget_mock.return_value.execute = AsyncMock(return_value=True)
            record = await service.start_run("demo.workflow.nightly", dry_run=False)
            await manager.join(record.run_id)

        with closing(history._connect()) as conn:
            step_ids = {
                row["step"]: row["run_step_id"]
                for row in conn.execute(
                    "SELECT step, run_step_id FROM run_steps WHERE run_id = ?",
                    (record.run_id,),
                ).fetchall()
            }

        assert [call[2].run_step_id for call in stats.calls] == [
            step_ids["backup.local.backup"],
            step_ids["backup.local.retention"],
        ]
        assert step_ids["backup.local.backup"] != step_ids["backup.local.retention"]

    asyncio.run(scenario())


def test_run_service_records_unexpected_error_raw(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_repo_config(config_path)
    manager = RunManager()
    service = RunService(ConfigService(config_path), manager, tmp_path / "locks", tmp_path / "logs")

    async def scenario() -> None:
        with patch(
            "src.services.runs._run_parsed_task_selector",
            side_effect=RuntimeError("boom secret-value"),
        ):
            record = await service.start_run("demo.backup.local")
            result = await manager.join(record.run_id)

        assert result.status == RunStatus.UNEXPECTED_ERROR
        assert result.error == "boom secret-value"

    asyncio.run(scenario())


def test_run_service_status_view_falls_back_to_scheduler(tmp_path: Path) -> None:
    manager = RunManager()
    scheduler_runs = [_scheduler_run_payload("sched-1")]
    client = _FakeRunControlClient(scheduler_runs)
    service = RunService(
        ConfigService(tmp_path / "config.toml"), manager, run_control_client=client
    )

    async def scenario() -> None:
        view = await service.get_run_status_view("sched-1")
        assert view["run_id"] == "sched-1"
        assert view["status"] == "running"
        assert view["origin"] == "scheduler"
        assert view["action_job"] == ""
        assert view["action_step"] == ""
        assert view["action_target"] is None

    asyncio.run(scenario())


def test_run_service_detail_view_prefers_scheduler_over_history(tmp_path: Path) -> None:
    history = RunHistoryService(tmp_path / "appdata.db")
    scheduler_runs = [_scheduler_run_payload("shared")]
    service = RunService(
        ConfigService(tmp_path / "config.toml"),
        RunManager(),
        run_control_client=_FakeRunControlClient(scheduler_runs),
        run_history_service=history,
    )

    async def scenario() -> None:
        await history.record(_history_record("shared", status=RunStatus.SUCCESS))

        view = await service.get_run_view("shared")

        assert view["origin"] == "scheduler"
        assert view["status"] == "running"
        assert view["is_active"] is True

    asyncio.run(scenario())


def test_run_service_status_view_falls_back_to_history_when_scheduler_unreachable(
    tmp_path: Path,
) -> None:
    history = RunHistoryService(tmp_path / "appdata.db")
    client = _FakeRunControlClient(
        list_error=ServiceError("scheduler_unreachable", "down", status_code=503)
    )
    service = RunService(
        ConfigService(tmp_path / "config.toml"),
        RunManager(),
        run_control_client=client,
        run_history_service=history,
    )

    async def scenario() -> None:
        await history.record(_history_record("hist-status", status=RunStatus.FAILED))

        view = await service.get_run_status_view("hist-status")

        assert view["run_id"] == "hist-status"
        assert view["status"] == "failed"
        assert view["is_active"] is False
        assert view["is_cancellable"] is False

    asyncio.run(scenario())


def test_run_service_historical_scheduler_status_view_has_no_run_actions(
    tmp_path: Path,
) -> None:
    history = RunHistoryService(tmp_path / "appdata.db")
    service = RunService(
        ConfigService(tmp_path / "config.toml"),
        RunManager(),
        run_history_service=history,
    )

    async def scenario() -> None:
        await history.record(_history_record("hist-scheduler", status=RunStatus.SUCCESS))

        view = await service.get_run_status_view(
            "hist-scheduler",
            action_job="demo",
            action_step="backup.local",
            with_actions=True,
        )

        assert view["origin"] == "scheduler"
        assert view["action_job"] == ""
        assert view["action_step"] == ""
        assert view["action_target"] is None

    asyncio.run(scenario())


def test_run_service_status_view_raises_not_found_when_absent_in_scheduler(
    tmp_path: Path,
) -> None:
    manager = RunManager()
    client = _FakeRunControlClient([])
    service = RunService(
        ConfigService(tmp_path / "config.toml"), manager, run_control_client=client
    )

    async def scenario() -> None:
        with pytest.raises(NotFoundServiceError):
            await service.get_run_status_view("missing")

    asyncio.run(scenario())


def test_run_service_status_view_raises_not_found_without_client(tmp_path: Path) -> None:
    manager = RunManager()
    service = RunService(ConfigService(tmp_path / "config.toml"), manager)

    async def scenario() -> None:
        with pytest.raises(NotFoundServiceError):
            await service.get_run_status_view("missing")

    asyncio.run(scenario())
