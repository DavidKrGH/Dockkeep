import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.services.config import ConfigService
from src.services.dashboard import DashboardService
from src.services.errors import ServiceError
from src.services.run_history import RunHistoryService
from src.services.run_manager import RunKind, RunOrigin, RunRecord, RunStatus

_CONFIG = """
[jobs.demo.backup.local]
repository = "/repo"
sources = ["/data"]
""".strip()


_SCHEDULED_CONFIG = """
[jobs.demo.backup.later_by_string]
repository = "/repo-a"
sources = ["/data"]
schedule = "30 1 * * *"

[jobs.demo.backup.earlier_by_time]
repository = "/repo-b"
sources = ["/data"]
schedule = "0 2 * * *"
""".strip()


class _FakeRunService:
    def __init__(
        self,
        active: set[str],
        *,
        scheduler_available: bool = True,
        error: Exception | None = None,
        active_run_views: list[dict[str, object]] | None = None,
    ) -> None:
        self._active = active
        self._scheduler_available = scheduler_available
        self._error = error
        self._active_run_views = active_run_views or []

    async def active_run_job_names(self) -> set[str]:
        if self._error is not None:
            raise self._error
        return self._active

    async def run_status_available(self) -> bool:
        if self._error is not None:
            raise self._error
        return self._scheduler_available

    async def list_active_runs_view(self) -> dict[str, object]:
        if self._error is not None:
            raise self._error
        return {
            "runs": self._active_run_views,
            "scheduler_available": self._scheduler_available,
        }


class _ExplodingConfigService:
    def load_active_config(self) -> object:
        raise AssertionError("job status view loaded config")


class _ExplodingBackupStatsService:
    def get_all_backup_stats(self) -> list[dict[str, object]]:
        raise AssertionError("job status view loaded stats")

    def get_dashboard_growth_data(self) -> dict[str, object]:
        raise AssertionError("job status view loaded growth data")


class _FakeBackupStatsService:
    def get_all_backup_stats(self) -> list[dict[str, object]]:
        return [
            {
                "job": "demo",
                "backup": "local",
                "repository_id": "repo-demo",
                "location_id": "loc-demo",
                "total_size_bytes": 2048,
                "snapshots_count": 3,
                "last_backup": {
                    "time": "2026-06-22T12:00:00+00:00",
                    "data_added_bytes": 512,
                    "duration_seconds": 9,
                },
            }
        ]

    def get_dashboard_growth_data(self) -> dict[str, object]:
        return {"all_labels": ["2026-06-22"], "datasets": [{"label": "demo.local"}]}


class _SharedRepositoryBackupStatsService:
    def get_all_backup_stats(self) -> list[dict[str, object]]:
        return [
            {
                "job": "demo",
                "backup": "one",
                "repository_id": "repo-shared",
                "location_id": "loc-one",
                "total_size_bytes": 2048,
                "snapshots_count": 3,
            },
            {
                "job": "demo",
                "backup": "two",
                "repository_id": "repo-shared",
                "location_id": "loc-two",
                "total_size_bytes": 2048,
                "snapshots_count": 3,
            },
        ]

    def get_dashboard_growth_data(self) -> dict[str, object]:
        return {"all_labels": [], "datasets": []}


def _service(
    tmp_path: Path,
    *,
    active: set[str],
    run_history_service: RunHistoryService | None = None,
    backup_stats_service: object | None = None,
    active_run_views: list[dict[str, object]] | None = None,
    config: str = _CONFIG,
) -> DashboardService:
    config_path = tmp_path / "config.toml"
    config_path.write_text(config, encoding="utf-8")
    return DashboardService(
        ConfigService(config_path),
        backup_stats_service=backup_stats_service,  # type: ignore[arg-type]
        run_service=_FakeRunService(active, active_run_views=active_run_views),  # type: ignore[arg-type]
        run_history_service=run_history_service,
    )


def test_dashboard_marks_job_running_from_active_runs(tmp_path: Path) -> None:
    service = _service(tmp_path, active={"demo"})

    view = asyncio.run(service.get_dashboard_view())
    job = view["jobs"][0]

    assert job["is_running"] is True
    assert job["backups"][0]["edit_url"] == "/config/jobs/demo/backups/local"


def test_dashboard_job_idle_without_active_run(tmp_path: Path) -> None:
    service = _service(tmp_path, active=set())

    view = asyncio.run(service.get_dashboard_view())
    job = view["jobs"][0]

    assert job["is_running"] is False
    assert job["run_status_unknown"] is False


def test_dashboard_marks_run_status_unknown_when_scheduler_unreachable(
    tmp_path: Path,
) -> None:
    service = DashboardService(
        ConfigService(_write_config(tmp_path, _CONFIG)),
        run_service=_FakeRunService(
            set(),
            error=ServiceError("scheduler_unreachable", "down", status_code=503),
        ),  # type: ignore[arg-type]
    )

    view = asyncio.run(service.get_dashboard_view())
    job = view["jobs"][0]

    assert view["scheduler_available"] is False
    assert view["run_status_unknown"] is True
    assert view["degraded"] is True
    assert job["is_running"] is False
    assert job["run_status_unknown"] is True


def test_job_status_view_uses_only_run_status() -> None:
    cases = [
        (
            _FakeRunService({"demo"}),
            {"name": "demo", "is_running": True, "run_status_unknown": False},
        ),
        (
            _FakeRunService(set()),
            {"name": "demo", "is_running": False, "run_status_unknown": False},
        ),
        (
            _FakeRunService({"demo"}, scheduler_available=False),
            {"name": "demo", "is_running": True, "run_status_unknown": True},
        ),
        (
            _FakeRunService(
                {"demo"},
                error=ServiceError("scheduler_unreachable", "down", status_code=503),
            ),
            {"name": "demo", "is_running": False, "run_status_unknown": True},
        ),
    ]

    for run_service, expected in cases:
        service = DashboardService(
            _ExplodingConfigService(),  # type: ignore[arg-type]
            backup_stats_service=_ExplodingBackupStatsService(),  # type: ignore[arg-type]
            run_service=run_service,  # type: ignore[arg-type]
        )

        assert asyncio.run(service.get_job_status_view("demo")) == expected


def test_dashboard_without_run_service_defaults_to_idle(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(_CONFIG, encoding="utf-8")
    service = DashboardService(ConfigService(config_path))

    view = asyncio.run(service.get_dashboard_view())
    job = view["jobs"][0]

    assert job["is_running"] is False


def test_dashboard_builds_operational_overview_and_stats_rows(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        active=set(),
        backup_stats_service=_FakeBackupStatsService(),
    )

    view = asyncio.run(service.get_dashboard_view())
    overview = view["overview"]
    backup = view["jobs"][0]["backups"][0]

    assert overview["total_repo_size_bytes"] == 2048
    assert overview["snapshots_count"] == 3
    assert overview["jobs_count"] == 1
    assert overview["backups_count"] == 1
    assert view["chart_data"]["growth"] == {
        "all_labels": ["2026-06-22"],
        "datasets": [{"label": "demo.local"}],
    }
    assert backup["total_size_bytes"] == 2048
    assert backup["snapshots_count"] == 3
    assert backup["last_backup_data_added_bytes"] == 512


def test_dashboard_deduplicates_storage_kpis_by_repository_id(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        active=set(),
        backup_stats_service=_SharedRepositoryBackupStatsService(),
        config="""
[jobs.demo.backup.one]
repository = "/repo-one"
sources = ["/data"]

[jobs.demo.backup.two]
repository = "/repo-two"
sources = ["/data"]
""".strip(),
    )

    view = asyncio.run(service.get_dashboard_view())

    assert view["overview"]["total_repo_size_bytes"] == 2048
    assert view["overview"]["snapshots_count"] == 3
    assert view["overview"]["backups_count"] == 2
    assert [backup["total_size_bytes"] for backup in view["jobs"][0]["backups"]] == [
        2048,
        2048,
    ]


def test_dashboard_exposes_recent_runs_and_failure_count(tmp_path: Path) -> None:
    history = RunHistoryService(tmp_path / "appdata.db")
    now = datetime.now(timezone.utc)

    async def scenario() -> None:
        await history.record(
            _record(
                "failed-run",
                status=RunStatus.FAILED,
                origin=RunOrigin.SCHEDULER,
                finished_at=now - timedelta(hours=2),
                error="boom",
            )
        )
        await history.record(
            _record(
                "success-run",
                status=RunStatus.SUCCESS,
                origin=RunOrigin.MANUAL,
                finished_at=now - timedelta(hours=1),
            )
        )
        await history.record(
            _record(
                "failed-run-old",
                status=RunStatus.FAILED,
                origin=RunOrigin.SCHEDULER,
                finished_at=now - timedelta(days=8),
            )
        )
        service = _service(tmp_path, active=set(), run_history_service=history)

        view = await service.get_dashboard_view()

        assert [run["run_id"] for run in view["recent_runs"]] == [
            "success-run",
            "failed-run",
            "failed-run-old",
        ]
        assert view["recent_runs"][0]["detail_url"] == "/runs/success-run"
        assert view["recent_runs"][0]["target_primary"] == "demo: local"
        assert view["recent_runs"][0]["task_type_label"] == "Backup"
        assert view["recent_runs"][0]["is_active"] is False
        assert view["overview"]["failures_7d"] == 1

    asyncio.run(scenario())


def test_dashboard_runs_panel_view_combines_upcoming_active_and_recent(tmp_path: Path) -> None:
    history = RunHistoryService(tmp_path / "appdata.db")
    now = datetime.now(timezone.utc)
    active_view = {"run_id": "live-1", "is_active": True, "cancel_url": "/runs/live-1/cancel"}

    async def scenario() -> None:
        await history.record(
            _record("done-1", status=RunStatus.SUCCESS, finished_at=now - timedelta(hours=1))
        )
        service = _service(
            tmp_path,
            active={"demo"},
            run_history_service=history,
            active_run_views=[active_view],
            config=_SCHEDULED_CONFIG,
        )

        panel = await service.get_runs_panel_view()
        view = await service.get_dashboard_view()

        assert panel["active_runs"] == [active_view]
        assert [run["run_id"] for run in panel["recent_runs"]] == ["done-1"]
        assert {run["name"] for run in panel["upcoming_runs"]} == {
            "later_by_string",
            "earlier_by_time",
        }
        assert {run["schedule"] for run in panel["upcoming_runs"]} == {
            "30 1 * * *",
            "0 2 * * *",
        }
        assert view["active_runs"] == [active_view]
        assert view["upcoming_runs"] == panel["upcoming_runs"]

    asyncio.run(scenario())


def test_dashboard_sorts_upcoming_runs_by_datetime_not_iso_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _write_config(tmp_path, _SCHEDULED_CONFIG)

    def fake_next_run_datetime(schedule: str | None) -> datetime | None:
        if schedule == "0 2 * * *":
            return datetime.fromisoformat("2026-01-01T02:00:00+02:00")
        if schedule == "30 1 * * *":
            return datetime(2026, 1, 1, 1, 30, tzinfo=timezone.utc)
        return None

    monkeypatch.setattr("src.services.dashboard.next_run_datetime", fake_next_run_datetime)
    service = DashboardService(ConfigService(config_path))

    view = asyncio.run(service.get_dashboard_view())

    assert [run["name"] for run in view["upcoming_runs"]] == [
        "earlier_by_time",
        "later_by_string",
    ]
    assert view["upcoming_runs"][0]["target_primary"] == "demo: earlier_by_time"
    assert view["upcoming_runs"][0]["target_secondary"] is None


def test_dashboard_uses_real_datetime_next_run_path(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, _SCHEDULED_CONFIG)
    service = DashboardService(ConfigService(config_path))

    view = asyncio.run(service.get_dashboard_view())

    assert len(view["upcoming_runs"]) == 2
    assert all(isinstance(run["next_run"], datetime) for run in view["upcoming_runs"])


def _record(
    run_id: str,
    *,
    status: RunStatus = RunStatus.SUCCESS,
    origin: RunOrigin = RunOrigin.MANUAL,
    finished_at: datetime | None = None,
    error: str | None = None,
) -> RunRecord:
    created_at = datetime(2026, 6, 22, 11, 0, tzinfo=timezone.utc)
    return RunRecord(
        run_id=run_id,
        origin=origin,
        run_kind=RunKind.JOB_TASK,
        job="demo",
        task_type="backup",
        task_name="local",
        status=status,
        dry_run=False,
        cancellable=False,
        created_at=created_at,
        started_at=created_at + timedelta(minutes=1),
        finished_at=finished_at or created_at + timedelta(minutes=2),
        error=error,
    )


def _write_config(tmp_path: Path, content: str) -> Path:
    config_path = tmp_path / "config.toml"
    config_path.write_text(content, encoding="utf-8")
    return config_path
