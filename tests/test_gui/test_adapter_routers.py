import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from src.services.errors import ServiceError
from src.services.restore import (
    RestoreCommand,
    RestoreMode,
    RestorePreview,
    RestoreRecord,
    RestoreRequest,
    RestoreStatus,
)

ROUTER_DIR = Path(__file__).resolve().parents[2] / "src" / "gui" / "routers"
TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "src" / "gui" / "templates"


def _request() -> MagicMock:
    request = MagicMock()
    request.query_params = {}
    request.is_disconnected = AsyncMock(return_value=False)
    backup_stats_mock = MagicMock()
    backup_stats_mock.get_all_backup_stats.return_value = []
    config_service = MagicMock()
    config_service.read_raw = AsyncMock(return_value={"content": "", "error": None, "warnings": []})
    config_service.save_raw = AsyncMock(return_value={"error": None, "warnings": []})
    run_service = MagicMock()
    run_service.start_run_status_view = AsyncMock(return_value={})
    run_service.list_runs_view = AsyncMock(return_value={"runs": []})
    run_service.get_run_view = AsyncMock(return_value={})
    run_service.get_run_status_view = AsyncMock(return_value={})
    run_service.cancel_run = AsyncMock(return_value=None)
    log_service = MagicMock()
    log_service.get_logs_view = AsyncMock(return_value={})
    log_service.read_raw = AsyncMock(return_value={"content": ""})
    rclone_service = MagicMock()
    rclone_service.get_rclone_view = AsyncMock(return_value={})
    rclone_service.get_remote_form = AsyncMock(return_value={})
    rclone_service.create_remote = AsyncMock(return_value={"remote_rows": [], "form": {}})
    rclone_service.update_remote = AsyncMock(return_value={"remote_rows": [], "form": {}})
    rclone_service.delete_remote = AsyncMock(return_value={"remote_rows": [], "form": {}})
    rclone_service.save_config_view = AsyncMock(return_value={})
    rclone_service.test_remote_view = AsyncMock(return_value={})
    repository_browser_service = MagicMock()
    repository_browser_service.get_repositories_view = AsyncMock(return_value={})
    repository_browser_service.get_location_snapshots_view = AsyncMock(return_value={})
    repository_browser_service.get_location_snapshot_view = AsyncMock(return_value={})
    repository_browser_service.browse_location_snapshot_view = AsyncMock(return_value={})
    repository_browser_service.prefetch_location_snapshot_view = AsyncMock(return_value={})
    repository_browser_service.refresh_targets_for_location = AsyncMock(return_value=[])
    restore_service = MagicMock()
    restore_service.get_restore_view = AsyncMock(return_value={})
    restore_service.cancel_restore = AsyncMock(return_value=None)
    dashboard_service = MagicMock()
    dashboard_service.get_dashboard_view = AsyncMock(return_value={})
    dashboard_service.get_runs_panel_view = AsyncMock(return_value={})
    dashboard_service.get_job_status_view = AsyncMock(return_value={})
    database_inspection_service = MagicMock()
    database_inspection_service.get_database_view = AsyncMock(return_value={})
    request.app.state.services = SimpleNamespace(
        config_service=config_service,
        run_service=run_service,
        log_service=log_service,
        rclone_service=rclone_service,
        repository_service=MagicMock(),
        repository_browser_service=repository_browser_service,
        restore_service=restore_service,
        dashboard_service=dashboard_service,
        backup_stats_service=backup_stats_mock,
        database_inspection_service=database_inspection_service,
    )

    def template_response(
        _request: object, template: str, context: dict[str, object], status_code: int = 200
    ) -> HTMLResponse:
        body = f"{template} {context}"
        return HTMLResponse(body, status_code=status_code)

    request.app.state.templates.TemplateResponse.side_effect = template_response
    return request


async def _with_async_to_thread(
    module: object,
    replacement: object,
    awaitable: object,
) -> object:
    original = module.asyncio.to_thread  # type: ignore[attr-defined]
    module.asyncio.to_thread = replacement  # type: ignore[attr-defined]
    try:
        return await awaitable  # type: ignore[misc]
    finally:
        module.asyncio.to_thread = original  # type: ignore[attr-defined]


def _rclone_view(**overrides: object) -> dict[str, object]:
    view: dict[str, object] = {
        "remotes": [],
        "remote_types": {},
        "remote_rows": [],
        "backend_choices": [],
        "supported_backend_types": (),
        "form": {},
        "result": None,
        "content": "",
        "conf_path": "/config/rclone.conf",
        "conf_missing": False,
        "error": None,
        "success": None,
    }
    view.update(overrides)
    return view


@pytest.mark.parametrize(
    ("router_attr", "step", "dry_run"),
    [
        ("run_job", "backup.local", False),
        ("run_dry_run_job", "backup.local", True),
        ("run_job", "workflow.nightly", False),
        ("run_dry_run_job", "workflow.nightly", True),
    ],
)
def test_jobs_run_routes_delegate_with_explicit_selector(
    router_attr: str, step: str, dry_run: bool
) -> None:
    import src.gui.routers.jobs as jobs_router

    request = _request()
    request.app.state.services.run_service.start_run_status_view.return_value = {
        "run_id": "run-1",
        "status": "running",
    }

    response = asyncio.run(getattr(jobs_router, router_attr)("demo", step, request))

    request.app.state.services.run_service.start_run_status_view.assert_called_once_with(
        f"demo.{step}",
        action_job="demo",
        action_step=step,
        dry_run=dry_run,
    )
    assert response.headers["HX-Trigger"] == "run-status-changed"


def test_runs_routes_delegate_to_run_service() -> None:
    from src.gui.routers.runs import (
        cancel_run,
        run_detail,
        run_status_fragment,
        runs_list_fragment,
        runs_page,
    )

    request = _request()
    request.app.state.services.run_service.list_runs_view.return_value = {
        "runs": [],
        "active_count": 0,
        "total_count": 0,
        "scheduler_available": False,
    }
    request.app.state.services.run_service.get_run_view.return_value = {"run_id": "run-1"}
    request.app.state.services.run_service.get_run_status_view.return_value = {"run_id": "run-1"}

    asyncio.run(runs_page(request))
    asyncio.run(runs_list_fragment(request))
    asyncio.run(run_detail("run-1", request))
    status_response = asyncio.run(run_status_fragment("run-1", request))
    cancel_response = asyncio.run(cancel_run("run-1", request))

    assert request.app.state.services.run_service.list_runs_view.call_count == 2
    request.app.state.services.run_service.list_runs_view.assert_any_call(
        page=1,
        page_size=50,
        job=None,
        task=None,
        status=None,
        origin=None,
    )
    request.app.state.services.run_service.list_runs_view.assert_called_with(
        page=1,
        page_size=50,
    )
    request.app.state.services.run_service.get_run_view.assert_called_once_with("run-1")
    request.app.state.services.run_service.cancel_run.assert_called_once_with("run-1")
    # runs_page + run_status_fragment + cancel_run each render a status viewmodel,
    # but only the fragment/cancel paths call get_run_status_view. No "actions" query
    # parameter was sent (default dashboard-poll case), so actions default to shown.
    assert request.app.state.services.run_service.get_run_status_view.call_count == 2
    request.app.state.services.run_service.get_run_status_view.assert_called_with(
        "run-1", with_actions=True
    )
    assert "HX-Trigger" not in status_response.headers
    assert cancel_response.headers["HX-Trigger"] == "run-status-changed"


def test_runs_page_passes_filters_to_run_service() -> None:
    from src.gui.routers.runs import runs_page

    request = _request()
    request.app.state.services.run_service.list_runs_view.return_value = {
        "runs": [],
        "active_runs": [],
        "history_runs": [],
        "active_count": 0,
        "history_count": 0,
        "total_count": 0,
        "scheduler_available": False,
        "filters": {
            "job": "demo",
            "task": "backup.local",
            "status": "failed",
            "origin": "manual",
        },
        "filter_options": {"jobs": [], "tasks": [], "statuses": [], "origins": []},
    }

    asyncio.run(
        runs_page(
            request,
            page=2,
            job="demo",
            task="backup.local",
            status="failed",
            origin="manual",
        )
    )

    request.app.state.services.run_service.list_runs_view.assert_called_once_with(
        page=2,
        page_size=50,
        job="demo",
        task="backup.local",
        status="failed",
        origin="manual",
    )


def test_runs_status_and_cancel_hide_actions_when_actions_query_param_is_zero() -> None:
    from src.gui.routers.runs import cancel_run, run_status_fragment

    request = _request()
    request.query_params = {"actions": "0"}
    request.app.state.services.run_service.get_run_status_view.return_value = {"run_id": "run-1"}

    asyncio.run(run_status_fragment("run-1", request))
    asyncio.run(cancel_run("run-1", request))

    request.app.state.services.run_service.get_run_status_view.assert_called_with(
        "run-1", with_actions=False
    )


def test_runs_cancel_route_renders_scheduler_status_for_dict_result() -> None:
    from src.gui.routers.runs import cancel_run

    request = _request()
    request.app.state.services.run_service.cancel_run.return_value = {
        "run_id": "sched-1",
        "status": "cancelled",
    }
    request.app.state.services.run_service.scheduler_status_view.return_value = {
        "run_id": "sched-1",
        "status": "cancelled",
        "status_label": "Cancelled",
    }

    asyncio.run(cancel_run("sched-1", request))

    request.app.state.services.run_service.cancel_run.assert_called_once_with("sched-1")
    request.app.state.services.run_service.scheduler_status_view.assert_called_once_with(
        {"run_id": "sched-1", "status": "cancelled"}
    )
    request.app.state.services.run_service.get_run_status_view.assert_not_called()


def test_config_save_uses_config_service_and_renders_result() -> None:
    from src.gui.routers import config as config_router

    request = _request()
    request.app.state.services.config_service.save_raw.return_value = {
        "content": "raw",
        "error": None,
        "success": "saved",
        "warnings": ["warn"],
    }
    rendered: dict[str, object] = {}

    def fake_render(
        request: object,
        content: str,
        error: str | None,
        success: str | None,
        warnings: object | None = None,
    ) -> MagicMock:
        rendered.update(
            {"content": content, "error": error, "success": success, "warnings": warnings}
        )
        return MagicMock()

    original_render = config_router._render
    try:
        config_router._render = fake_render  # type: ignore[assignment]
        asyncio.run(config_router.save_config(request, "raw"))
    finally:
        config_router._render = original_render  # type: ignore[assignment]

    request.app.state.services.config_service.save_raw.assert_called_once_with("raw")
    assert rendered == {"content": "raw", "error": None, "success": "saved", "warnings": ["warn"]}


def test_config_landing_redirects_to_structured_editor() -> None:
    from src.gui.routers.config import config_page

    response = asyncio.run(config_page())

    assert response.status_code == 303
    assert response.headers["location"] == "/config/jobs"


def test_logs_raw_uses_log_service() -> None:
    from src.gui.routers.diagnostics import raw_log

    request = _request()
    request.query_params = {"date": "2026-05-24"}
    request.app.state.services.log_service.read_raw.return_value = {"content": "line"}

    response = asyncio.run(raw_log("demo", request))

    request.app.state.services.log_service.read_raw.assert_called_once_with(
        "demo", "2026-05-24", tail=0
    )
    assert response.body.decode() == "line"


@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (ServiceError("log_not_found", "Log file not found", 404), 404),
        (ServiceError("invalid_date", "Date must be YYYY-MM-DD", 400), 400),
    ],
)
def test_logs_raw_preserves_service_error_status(error: ServiceError, status_code: int) -> None:
    from src.gui.routers.diagnostics import raw_log

    request = _request()
    request.query_params = {"date": "invalid"}
    request.app.state.services.log_service.read_raw.side_effect = error

    response = asyncio.run(raw_log("demo", request))

    assert response.status_code == status_code
    assert response.body.decode() == error.message


def test_logs_stream_maps_validation_error_before_streaming() -> None:
    from src.gui.routers.diagnostics import stream_logs

    request = _request()
    request.app.state.services.log_service.open_stream.side_effect = ServiceError(
        "invalid_job", "Invalid job name", 400
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(stream_logs("demo", request))

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == {"code": "invalid_job", "message": "Invalid job name"}


def test_logs_stream_emits_named_error_event_for_runtime_service_error() -> None:
    from src.gui.routers.diagnostics import stream_logs

    async def failing_stream() -> AsyncIterator[str]:
        yield 'data: {"line": "first"}\n\n'
        raise ServiceError("read_error", "stream failed", 500)

    async def collect_events() -> list[dict[str, str]]:
        request = _request()
        request.app.state.services.log_service.open_stream.return_value = failing_stream()
        response = await stream_logs("demo", request)
        return [event async for event in response.body_iterator]

    events = asyncio.run(collect_events())

    assert events[0] == {"data": "first"}
    assert events[1]["event"] == "error"
    assert json.loads(events[1]["data"]) == {
        "code": "read_error",
        "message": "stream failed",
    }


def test_log_routes_reject_invalid_job_name_before_service_call() -> None:
    from src.gui.routers.diagnostics import router as diagnostics_router

    app = FastAPI()
    app.include_router(diagnostics_router)
    with TestClient(app) as client:
        raw_response = client.get("/diagnostics/logs/not.valid/raw")
        stream_response = client.get("/diagnostics/logs/not.valid/stream")

    assert raw_response.status_code == 422
    assert stream_response.status_code == 422


def test_database_route_delegates_to_inspection_service() -> None:
    from src.gui.routers.diagnostics import database_page

    request = _request()
    request.query_params = {
        "section": "runs",
        "table": "runs",
        "page": "2",
        "filter_column": "run_id",
        "filter_value": "run-1",
    }
    request.app.state.services.database_inspection_service.get_database_view.return_value = {
        "active_section": "runs",
        "active_table": "runs",
    }

    response = asyncio.run(database_page(request))

    request.app.state.services.database_inspection_service.get_database_view.assert_called_once_with(
        section="runs",
        table="runs",
        page=2,
        filter_column="run_id",
        filter_value="run-1",
    )
    assert response.status_code == 200
    assert "diagnostics_database.html" in response.body.decode()


def test_job_run_routes_reject_invalid_job_name_before_service_call() -> None:
    from src.gui.routers.jobs import router as jobs_router

    app = FastAPI()
    app.include_router(jobs_router)
    with TestClient(app) as client:
        run_response = client.post("/jobs/not.valid/backup.local/run")
        dry_run_response = client.post("/jobs/not.valid/backup.local/dry-run")

    assert run_response.status_code == 422
    assert dry_run_response.status_code == 422


def test_rclone_remote_test_uses_rclone_service() -> None:
    from src.gui.routers.rclone import test_remote

    request = _request()
    request.app.state.services.rclone_service.test_remote_view = AsyncMock(
        return_value={
            "status": "ok",
            "tone": "success",
            "detail": None,
            "symbol": "OK",
            "label": "Reachable",
        }
    )

    asyncio.run(test_remote("cloud", request))

    request.app.state.services.rclone_service.test_remote_view.assert_called_once_with("cloud")


def test_rclone_remote_test_preserves_service_error_status() -> None:
    from src.gui.routers.rclone import test_remote

    request = _request()
    request.app.state.services.rclone_service.test_remote_view = AsyncMock(
        side_effect=ServiceError("invalid_parameter", "Invalid rclone remote name", 400)
    )

    with pytest.raises(ServiceError) as exc_info:
        asyncio.run(test_remote("--config=/tmp/x", request))

    assert exc_info.value.status_code == 400
    assert exc_info.value.code == "invalid_parameter"
    assert exc_info.value.message == "Invalid rclone remote name"


def test_rclone_page_does_not_swallow_unexpected_errors() -> None:
    from src.gui.routers.rclone import rclone_page

    request = _request()
    request.app.state.services.rclone_service.get_rclone_view = AsyncMock(
        side_effect=RuntimeError("read failed")
    )

    with pytest.raises(RuntimeError, match="read failed"):
        asyncio.run(rclone_page(request))


def test_rclone_page_renders_read_error_outside_editor_content() -> None:
    from src.gui.routers import rclone as rclone_router

    request = _request()
    request.app.state.services.rclone_service.get_rclone_view = AsyncMock(
        return_value=_rclone_view(
            conf_path="/config/rclone.conf",
            content="",
            conf_missing=True,
            error={"code": "read_error", "message": "permission denied"},
        )
    )
    rendered: dict[str, object] = {}

    def fake_render(
        request: object,
        template: str,
        data: dict[str, object],
    ) -> MagicMock:
        rendered.update(
            {
                "template": template,
                "content": data["content"],
                "error": data["error"],
                "conf_missing": data["conf_missing"],
            }
        )
        return MagicMock()

    original_render = rclone_router._render_page
    try:
        rclone_router._render_page = fake_render  # type: ignore[assignment]
        asyncio.run(rclone_router.rclone_page(request))
    finally:
        rclone_router._render_page = original_render  # type: ignore[assignment]

    assert rendered["template"] == "rclone.html"
    assert rendered["content"] == ""
    assert rendered["error"] == {"code": "read_error", "message": "permission denied"}
    assert rendered["conf_missing"] is True


def test_rclone_raw_page_renders_raw_editor() -> None:
    from src.gui.routers.rclone import rclone_raw_page

    request = _request()
    request.app.state.services.rclone_service.get_rclone_view = AsyncMock(
        return_value=_rclone_view(content="[remote]\ntype = s3\n")
    )

    response = asyncio.run(rclone_raw_page(request))

    request.app.state.services.rclone_service.get_rclone_view.assert_called_once_with()
    assert response.status_code == 200
    assert "rclone_raw.html" in response.body.decode()


def test_rclone_router_exposes_config_remotes_only() -> None:
    from src.gui.routers.rclone import router

    routes = {
        getattr(route, "path", None)
        for route in router.routes
        if getattr(route, "path", None) is not None
    }

    assert router.prefix == "/config/remotes"
    assert "/config/remotes" in routes
    assert "/config/remotes/raw" in routes
    assert "/config/remotes/form" in routes
    assert "/config/remotes/{remote}/test" in routes
    assert "/rclone" not in routes
    assert "/rclone/raw" not in routes
    assert "/rclone/form" not in routes
    assert "/rclone/{remote}/test" not in routes


def test_rclone_form_route_delegates_to_service() -> None:
    from src.gui.routers.rclone import remote_form

    request = _request()
    request.query_params = {
        "remote_type": "sftp",
        "remote": "nas",
        "remote_name": "ignored-in-edit",
        "host": "example.com",
    }
    request.app.state.services.rclone_service.get_remote_form = AsyncMock(
        return_value={"supported": True, "fields": []}
    )

    asyncio.run(remote_form(request, remote_type="sftp", remote="nas"))

    request.app.state.services.rclone_service.get_remote_form.assert_called_once_with(
        "nas",
        "sftp",
        {"name": "ignored-in-edit", "host": "example.com"},
    )


def test_rclone_create_update_delete_delegate_to_service() -> None:
    from src.gui.routers.rclone import create_remote, delete_remote, update_remote

    request = _request()
    request.headers = {}
    request.form = AsyncMock(
        return_value={
            "remote_name": "nas",
            "remote_type": "sftp",
            "host": "example.com",
        }
    )
    service = request.app.state.services.rclone_service
    service.create_remote = AsyncMock(return_value=_rclone_view())
    service.update_remote = AsyncMock(return_value=_rclone_view())
    service.delete_remote = AsyncMock(return_value=_rclone_view())

    asyncio.run(create_remote(request))
    service.create_remote.assert_called_once_with(
        "nas",
        "sftp",
        {"host": "example.com"},
    )

    request.form = AsyncMock(return_value={"host": "changed.example"})
    asyncio.run(update_remote("nas", request))
    service.update_remote.assert_called_once_with("nas", {"host": "changed.example"})

    asyncio.run(delete_remote("nas", request))
    service.delete_remote.assert_called_once_with("nas")


def test_rclone_create_update_delete_success_redirects_to_overview() -> None:
    from src.gui.routers.rclone import create_remote, delete_remote, update_remote

    request = _request()
    request.form = AsyncMock(
        return_value={
            "remote_name": "nas",
            "remote_type": "sftp",
            "host": "example.com",
        }
    )
    service = request.app.state.services.rclone_service
    service.create_remote = AsyncMock(return_value=_rclone_view(result={"ok": True}))
    service.update_remote = AsyncMock(return_value=_rclone_view(result={"ok": True}))
    service.delete_remote = AsyncMock(return_value=_rclone_view(result={"ok": True}))

    create_response = asyncio.run(create_remote(request))
    request.form = AsyncMock(return_value={"host": "changed.example"})
    update_response = asyncio.run(update_remote("nas", request))
    delete_response = asyncio.run(delete_remote("nas", request))

    assert create_response.status_code == 303
    assert create_response.headers["location"] == "/config/remotes"
    assert update_response.status_code == 303
    assert update_response.headers["location"] == "/config/remotes"
    assert delete_response.status_code == 303
    assert delete_response.headers["location"] == "/config/remotes"


def test_repositories_routes_use_repository_browser_service() -> None:
    from src.gui.routers.repositories import (
        repositories_page,
        repository_location_delete,
        repository_location_merge,
    )

    request = _request()
    request.app.state.services.repository_browser_service.get_repositories_view.return_value = {
        "repositories": [{"job": "demo", "backup": "local"}],
    }

    asyncio.run(repositories_page(request))
    request.form = AsyncMock(return_value={"repository_id": "repo-id"})
    response = asyncio.run(repository_location_delete("loc-id", request))
    request.form = AsyncMock(
        return_value={"repository_id": "repo-id", "target_location_id": "target-loc-id"}
    )
    merge_response = asyncio.run(repository_location_merge("source-loc-id", request))

    service = request.app.state.services.repository_browser_service
    service.get_repositories_view.assert_called_once_with()
    request.app.state.services.repository_service.ensure_backup.assert_not_called()
    request.app.state.services.repository_service.delete_location.assert_called_once_with(
        "repo-id", "loc-id"
    )
    request.app.state.services.repository_service.merge_location.assert_called_once_with(
        "repo-id", "source-loc-id", "target-loc-id"
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/repositories"
    assert merge_response.status_code == 303
    assert merge_response.headers["location"] == "/repositories"


@pytest.mark.parametrize(
    ("route_name", "error", "status_code"),
    [
        ("delete", ServiceError("repository_location_not_found", "missing", 404), 404),
        ("merge", ServiceError("repository_location_not_found", "missing", 404), 404),
    ],
)
def test_repository_routes_map_service_errors(
    route_name: str, error: ServiceError, status_code: int
) -> None:
    from src.gui.routers.repositories import (
        repository_location_delete,
        repository_location_merge,
    )

    request = _request()
    if route_name == "delete":
        request.form = AsyncMock(return_value={"repository_id": "repo-id"})
        request.app.state.services.repository_service.delete_location.side_effect = error
        call = repository_location_delete("loc-id", request)
    else:
        request.form = AsyncMock(
            return_value={"repository_id": "repo-id", "target_location_id": "target-loc-id"}
        )
        request.app.state.services.repository_service.merge_location.side_effect = error
        call = repository_location_merge("loc-id", request)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(call)

    assert exc_info.value.status_code == status_code
    assert exc_info.value.detail == {"code": error.code, "message": error.message}


def test_repository_config_routes_are_not_registered() -> None:
    from src.gui.routers.repositories import router as repositories_router

    app = FastAPI()
    app.include_router(repositories_router)
    with TestClient(app) as client:
        namespaced = client.get("/repositories/configs/demo/local/snapshots")
        legacy = client.get("/repositories/demo/local/snapshots")

    assert namespaced.status_code == 404
    assert legacy.status_code == 404


def test_repository_locations_route_does_not_match_legacy_config_redirect() -> None:
    from src.gui.routers.repositories import router as repositories_router

    request = _request()
    service = request.app.state.services.repository_browser_service
    service.get_location_snapshots_view.return_value = {
        "location_id": "loc-1",
        "snapshots": [],
        "cache_empty": False,
    }
    app = FastAPI()
    app.state.services = request.app.state.services
    app.state.templates = request.app.state.templates
    app.include_router(repositories_router)

    with TestClient(app) as client:
        response = client.get("/repositories/locations/loc-1/snapshots")

    assert response.status_code == 200
    service.get_location_snapshots_view.assert_called_once_with("loc-1")


def test_repository_location_routes_use_repository_browser_service() -> None:
    from src.gui.routers.repositories import (
        repository_location_snapshot_browse,
        repository_location_snapshot_detail,
        repository_location_snapshot_prefetch,
        repository_location_snapshots,
    )

    request = _request()
    service = request.app.state.services.repository_browser_service
    service.get_location_snapshots_view.return_value = {
        "location_id": "loc-1",
        "snapshots": [{"id": "abc"}],
    }
    service.get_location_snapshot_view.return_value = {
        "location_id": "loc-1",
        "snapshot": {"id": "abc"},
    }
    service.browse_location_snapshot_view = AsyncMock(
        return_value={
            "location_id": "loc-1",
            "snapshot_id": "abc",
            "path": "/",
            "entries": [],
        }
    )

    asyncio.run(repository_location_snapshots("loc-1", request))
    asyncio.run(repository_location_snapshot_detail("loc-1", "abc", request))
    request.query_params = {"path": "/data"}
    asyncio.run(repository_location_snapshot_browse("loc-1", "abc", request, page=2))
    request.query_params = {"path": "/data"}
    asyncio.run(repository_location_snapshot_prefetch("loc-1", "abc", request, page=2))

    service.get_location_snapshots_view.assert_called_once_with("loc-1")
    service.get_location_snapshot_view.assert_called_once_with("loc-1", "abc")
    service.browse_location_snapshot_view.assert_called_once_with(
        location_id="loc-1", snapshot_id="abc", path="/data", page=2
    )
    service.prefetch_location_snapshot_view.assert_called_once_with(
        location_id="loc-1", snapshot_id="abc", path="/data", page=2
    )


@pytest.mark.parametrize(
    ("route_name", "error", "status_code"),
    [
        ("snapshots", ServiceError("not_found", "missing", 404), 404),
        ("detail", ServiceError("not_found", "missing", 404), 404),
        ("browse", ServiceError("invalid_page", "bad page", 400), 400),
        ("prefetch", ServiceError("invalid_page", "bad page", 400), 400),
    ],
)
def test_repository_location_routes_map_service_errors(
    route_name: str, error: ServiceError, status_code: int
) -> None:
    from src.gui.routers.repositories import (
        repository_location_snapshot_browse,
        repository_location_snapshot_detail,
        repository_location_snapshot_prefetch,
        repository_location_snapshots,
    )

    request = _request()
    service = request.app.state.services.repository_browser_service
    if route_name == "snapshots":
        service.get_location_snapshots_view.side_effect = error
        call = repository_location_snapshots("loc-1", request)
    elif route_name == "detail":
        service.get_location_snapshot_view.side_effect = error
        call = repository_location_snapshot_detail("loc-1", "abc", request)
    else:
        if route_name == "prefetch":
            service.prefetch_location_snapshot_view = AsyncMock(side_effect=error)
            call = repository_location_snapshot_prefetch("loc-1", "abc", request)
        else:
            service.browse_location_snapshot_view = AsyncMock(side_effect=error)
            call = repository_location_snapshot_browse("loc-1", "abc", request)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(call)

    assert exc_info.value.status_code == status_code
    assert exc_info.value.detail == {"code": error.code, "message": error.message}


def test_repository_location_snapshots_refresh_triggers_collect_and_redirects() -> None:
    from src.gui.routers.repositories import repository_location_snapshots_refresh

    request = _request()
    refresh_targets = (
        request.app.state.services.repository_browser_service.refresh_targets_for_location
    )
    refresh_targets.return_value = [("job1", "backup1"), ("job2", "backup2")]
    request.app.state.services.backup_stats_service.refresh_backup_stats = AsyncMock()

    response = asyncio.run(repository_location_snapshots_refresh("loc-1", request))

    assert response.status_code == 303
    assert response.headers["location"] == "/repositories/locations/loc-1/snapshots"
    request.app.state.services.repository_browser_service.refresh_targets_for_location.assert_awaited_once_with(
        "loc-1"
    )
    request.app.state.services.backup_stats_service.refresh_backup_stats.assert_any_await(
        "job1", "backup1"
    )
    request.app.state.services.backup_stats_service.refresh_backup_stats.assert_any_await(
        "job2", "backup2"
    )


def test_repository_location_snapshots_refresh_unknown_location_maps_to_404() -> None:
    from src.gui.routers.repositories import repository_location_snapshots_refresh
    from src.services.errors import NotFoundServiceError

    request = _request()
    request.app.state.services.repository_browser_service.refresh_targets_for_location = AsyncMock(
        side_effect=NotFoundServiceError("Location not found: loc-missing")
    )
    request.app.state.services.backup_stats_service.refresh_backup_stats = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(repository_location_snapshots_refresh("loc-missing", request))

    assert exc_info.value.status_code == 404
    request.app.state.services.backup_stats_service.refresh_backup_stats.assert_not_called()


def test_dashboard_empty_state_links_target_root_dashboard() -> None:
    for relative_path in (
        "fragments/runs_history.html",
        "repository_location_snapshots.html",
    ):
        content = (TEMPLATE_DIR / relative_path).read_text(encoding="utf-8")
        assert 'href="/dashboard"' not in content
        assert 'href="/"' in content


def test_repositories_refresh_triggers_refresh_all_and_redirects() -> None:
    from src.gui.routers.repositories import repositories_refresh

    request = _request()
    request.app.state.services.backup_stats_service.refresh_all_backup_stats = AsyncMock()

    response = asyncio.run(repositories_refresh(request))

    assert response.status_code == 303
    assert response.headers["location"] == "/repositories"
    request.app.state.services.backup_stats_service.refresh_all_backup_stats.assert_awaited_once_with()


def test_repository_location_delete_runs_store_write_off_event_loop() -> None:
    from src.gui.routers import repositories as repositories_router

    request = _request()
    request.form = AsyncMock(return_value={"repository_id": "repo-1"})
    calls: list[tuple[object, tuple[object, ...]]] = []

    async def immediate(func: object, *args: object) -> object:
        calls.append((func, args))
        return func(*args)  # type: ignore[operator]

    response = asyncio.run(
        _with_async_to_thread(
            repositories_router,
            immediate,
            repositories_router.repository_location_delete("loc-1", request),
        )
    )

    assert response.status_code == 303
    assert calls == [
        (request.app.state.services.repository_service.delete_location, ("repo-1", "loc-1"))
    ]
    request.app.state.services.repository_service.delete_location.assert_called_once_with(
        "repo-1", "loc-1"
    )


def test_repository_location_merge_runs_store_write_off_event_loop() -> None:
    from src.gui.routers import repositories as repositories_router

    request = _request()
    request.form = AsyncMock(
        return_value={"repository_id": "repo-1", "target_location_id": "loc-2"}
    )
    calls: list[tuple[object, tuple[object, ...]]] = []

    async def immediate(func: object, *args: object) -> object:
        calls.append((func, args))
        return func(*args)  # type: ignore[operator]

    response = asyncio.run(
        _with_async_to_thread(
            repositories_router,
            immediate,
            repositories_router.repository_location_merge("loc-1", request),
        )
    )

    assert response.status_code == 303
    assert calls == [
        (
            request.app.state.services.repository_service.merge_location,
            ("repo-1", "loc-1", "loc-2"),
        )
    ]
    request.app.state.services.repository_service.merge_location.assert_called_once_with(
        "repo-1", "loc-1", "loc-2"
    )


def _restore_record(status: str = "queued") -> RestoreRecord:
    return RestoreRecord(
        restore_id=f"restore-{status}",
        request=RestoreRequest(
            job="demo",
            backup="local",
            snapshot_id="abcdef1234567890",
            mode=RestoreMode.BROWSER,
            snapshot_paths=("/", "/data"),
            restore_target=Path("/restore/demo/local/abcdef12/20260527T120000Z"),
            overwrite=False,
        ),
        status=RestoreStatus(status),
        created_at=datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc),
        output="restored <file>",
        error="raw <error>" if status in {"failed", "config_error"} else None,
    )


def test_restore_preview_calls_service_with_form_parameters() -> None:
    from src.gui.routers.restore import preview_restore

    request = _request()
    preview = RestorePreview(
        request=RestoreRequest(
            job="demo",
            backup="local",
            snapshot_id="abcdef1234567890",
            mode=RestoreMode.BROWSER,
            snapshot_paths=("/data", "/etc"),
            restore_target=Path("/restore/demo/custom"),
            overwrite=True,
        ),
        command=RestoreCommand(
            argv=[
                "restic",
                "--repo",
                "/repo",
                "restore",
                "abcdef1234567890",
                "--target",
                "/restore/demo/custom",
                "--dry-run",
            ],
            restore_target=Path("/restore/demo/custom"),
            mode=RestoreMode.BROWSER,
            snapshot_paths=("/data", "/etc"),
            include_patterns=(),
            exclude_patterns=(),
            overwrite=True,
            dry_run=True,
        ),
        ok=True,
        output="would restore",
        output_truncated=False,
    )
    request.app.state.services.restore_service.preview_restore = AsyncMock(return_value=preview)
    request.app.state.services.restore_service.preview_view.return_value = {"ok": True}

    asyncio.run(
        preview_restore(
            request,
            job_name="demo",
            backup_name="local",
            location_id="",
            snapshot_id="abcdef1234567890",
            restore_mode="browser",
            snapshot_paths="/data\n/etc",
            include_patterns="",
            exclude_patterns="",
            target="/restore/demo/custom",
            overwrite=True,
            adhoc_password="",
            adhoc_password_env="",
            adhoc_password_file="",
        )
    )

    request.app.state.services.restore_service.preview_restore.assert_awaited_once_with(
        "demo",
        "local",
        "abcdef1234567890",
        mode="browser",
        snapshot_paths=["/data", "/etc"],
        include_patterns=None,
        exclude_patterns=None,
        target="/restore/demo/custom",
        overwrite=True,
        resolved_backup=None,
    )


def test_restore_preview_passes_pattern_mode_lines_to_service() -> None:
    from src.gui.routers.restore import preview_restore

    request = _request()
    request.app.state.services.restore_service.preview_restore = AsyncMock(return_value=MagicMock())
    request.app.state.services.restore_service.preview_view.return_value = {"ok": True}

    asyncio.run(
        preview_restore(
            request,
            job_name="demo",
            backup_name="local",
            location_id="",
            snapshot_id="abcdef1234567890",
            restore_mode="pattern",
            snapshot_paths="",
            include_patterns=" /data/** \n\n*.jpg",
            exclude_patterns="",
            target="",
            overwrite=False,
            adhoc_password="",
            adhoc_password_env="",
            adhoc_password_file="",
        )
    )

    request.app.state.services.restore_service.preview_restore.assert_awaited_once_with(
        "demo",
        "local",
        "abcdef1234567890",
        mode="pattern",
        snapshot_paths=None,
        include_patterns=["/data/**", "*.jpg"],
        exclude_patterns=None,
        target=None,
        overwrite=False,
        resolved_backup=None,
    )


def test_restore_start_calls_async_service_with_form_parameters() -> None:
    from src.gui.routers.restore import start_restore

    request = _request()
    request.app.state.services.restore_service.start_restore = AsyncMock(
        return_value=_restore_record("queued")
    )
    request.app.state.services.restore_service.restore_view.return_value = {"status": "queued"}

    response = asyncio.run(
        start_restore(
            request,
            job_name="demo",
            backup_name="local",
            location_id="",
            snapshot_id="abcdef1234567890",
            restore_mode="browser",
            snapshot_paths="/data/a,b.txt\n/etc",
            include_patterns="",
            exclude_patterns="",
            target="",
            overwrite=False,
            adhoc_password="",
            adhoc_password_env="",
            adhoc_password_file="",
        )
    )

    request.app.state.services.restore_service.start_restore.assert_awaited_once_with(
        "demo",
        "local",
        "abcdef1234567890",
        mode="browser",
        snapshot_paths=["/data/a,b.txt", "/etc"],
        include_patterns=None,
        exclude_patterns=None,
        target=None,
        overwrite=False,
        resolved_backup=None,
    )
    assert response.headers["HX-Trigger"] == "run-status-changed"


def test_restore_cancel_route_calls_service_and_renders_status_fragment() -> None:
    from src.gui.routers.restore import cancel_restore

    request = _request()
    request.app.state.services.restore_service.cancel_restore = AsyncMock()
    request.app.state.services.restore_service.get_restore_view = AsyncMock(
        return_value={"run_id": "restore-running", "status": "cancelled"}
    )

    response = asyncio.run(cancel_restore("restore-running", request))

    request.app.state.services.restore_service.cancel_restore.assert_awaited_once_with(
        "restore-running"
    )
    request.app.state.services.restore_service.get_restore_view.assert_awaited_once_with(
        "restore-running"
    )
    assert response.headers["HX-Trigger"] == "run-status-changed"


def test_restore_status_route_does_not_trigger_global_run_refresh() -> None:
    from src.gui.routers.restore import restore_status_fragment

    request = _request()
    request.app.state.services.restore_service.get_restore_view = AsyncMock(
        return_value={"run_id": "restore-running", "status": "running"}
    )

    response = asyncio.run(restore_status_fragment("restore-running", request))

    request.app.state.services.restore_service.get_restore_view.assert_awaited_once_with(
        "restore-running"
    )
    assert "HX-Trigger" not in response.headers


def test_restore_preview_disconnect_cancels_and_awaits_preview_cleanup() -> None:
    from src.gui.routers.restore import preview_restore

    request = _request()
    preview_started = asyncio.Event()
    cleanup_complete = asyncio.Event()

    async def wait_for_cancellation(*_args: object, **_kwargs: object) -> None:
        preview_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_complete.set()

    request.app.state.services.restore_service.preview_restore = wait_for_cancellation
    request.is_disconnected = AsyncMock(side_effect=[False, True])

    async def run_preview() -> None:
        with pytest.raises(asyncio.CancelledError):
            await preview_restore(
                request,
                job_name="demo",
                backup_name="local",
                location_id="",
                snapshot_id="abcdef1234567890",
                restore_mode="pattern",
                snapshot_paths="",
                include_patterns="",
                exclude_patterns="",
                target="",
                overwrite=False,
                adhoc_password="",
                adhoc_password_env="",
                adhoc_password_file="",
            )

    asyncio.run(run_preview())

    assert preview_started.is_set()
    assert cleanup_complete.is_set()
    assert request.is_disconnected.await_count == 2


def test_restore_service_error_renders_html_fragment_not_json() -> None:
    from src.gui.routers.restore import preview_restore
    from src.services.errors import ServiceError

    request = _request()
    request.app.state.services.restore_service.preview_restore = AsyncMock(
        side_effect=ServiceError(
            "invalid_restore_target",
            "Restore target must be under restore base",
            status_code=400,
        )
    )

    response = asyncio.run(
        preview_restore(
            request,
            job_name="demo",
            backup_name="local",
            location_id="",
            snapshot_id="abcdef1234567890",
            restore_mode="pattern",
            snapshot_paths="",
            include_patterns="",
            exclude_patterns="",
            target="/tmp/nope",
            overwrite=False,
            adhoc_password="",
            adhoc_password_env="",
            adhoc_password_file="",
        )
    )

    # GUI invariant: service errors render as an HTML fragment, never as a JSON API body.
    assert response.status_code == 200
    assert response.media_type == "text/html"
    assert not response.body.decode().lstrip().startswith("{")


@pytest.mark.parametrize(
    "route",
    ["preview", "start"],
)
def test_restore_routes_resolve_location_id_and_ignore_job_backup_fields(route: str) -> None:
    from src.gui.routers.restore import preview_restore, start_restore

    request = _request()
    override = MagicMock(name="resolved_backup_override")
    browser_service = request.app.state.services.repository_browser_service
    browser_service.resolve_backup_config_for_location.return_value = (
        "donor-job",
        "donor-backup",
        override,
    )

    if route == "preview":
        request.app.state.services.restore_service.preview_restore = AsyncMock(
            return_value=MagicMock()
        )
        request.app.state.services.restore_service.preview_view.return_value = {"ok": True}
        asyncio.run(
            preview_restore(
                request,
                job_name="client-supplied-job",
                backup_name="client-supplied-backup",
                location_id="loc-1",
                snapshot_id="abcdef1234567890",
                restore_mode="pattern",
                snapshot_paths="",
                include_patterns="",
                exclude_patterns="",
                target="",
                overwrite=False,
                adhoc_password="",
                adhoc_password_env="",
                adhoc_password_file="",
            )
        )
        call = request.app.state.services.restore_service.preview_restore
    else:
        request.app.state.services.restore_service.start_restore = AsyncMock(
            return_value=_restore_record("queued")
        )
        request.app.state.services.restore_service.restore_view.return_value = {"status": "queued"}
        asyncio.run(
            start_restore(
                request,
                job_name="client-supplied-job",
                backup_name="client-supplied-backup",
                location_id="loc-1",
                snapshot_id="abcdef1234567890",
                restore_mode="pattern",
                snapshot_paths="",
                include_patterns="",
                exclude_patterns="",
                target="",
                overwrite=False,
                adhoc_password="",
                adhoc_password_env="",
                adhoc_password_file="",
            )
        )
        call = request.app.state.services.restore_service.start_restore

    request.app.state.services.repository_browser_service.resolve_backup_config_for_location.assert_called_once_with(
        "loc-1"
    )
    call.assert_awaited_once_with(
        "donor-job",
        "donor-backup",
        "abcdef1234567890",
        mode="pattern",
        snapshot_paths=None,
        include_patterns=None,
        exclude_patterns=None,
        target=None,
        overwrite=False,
        resolved_backup=override,
    )


@pytest.mark.parametrize(
    "route",
    ["preview", "start"],
)
def test_restore_routes_without_location_id_behave_as_before(route: str) -> None:
    from src.gui.routers.restore import preview_restore, start_restore

    request = _request()

    if route == "preview":
        request.app.state.services.restore_service.preview_restore = AsyncMock(
            return_value=MagicMock()
        )
        request.app.state.services.restore_service.preview_view.return_value = {"ok": True}
        asyncio.run(
            preview_restore(
                request,
                job_name="demo",
                backup_name="local",
                location_id="",
                snapshot_id="abcdef1234567890",
                restore_mode="pattern",
                snapshot_paths="",
                include_patterns="",
                exclude_patterns="",
                target="",
                overwrite=False,
                adhoc_password="",
                adhoc_password_env="",
                adhoc_password_file="",
            )
        )
        call = request.app.state.services.restore_service.preview_restore
    else:
        request.app.state.services.restore_service.start_restore = AsyncMock(
            return_value=_restore_record("queued")
        )
        request.app.state.services.restore_service.restore_view.return_value = {"status": "queued"}
        asyncio.run(
            start_restore(
                request,
                job_name="demo",
                backup_name="local",
                location_id="",
                snapshot_id="abcdef1234567890",
                restore_mode="pattern",
                snapshot_paths="",
                include_patterns="",
                exclude_patterns="",
                target="",
                overwrite=False,
                adhoc_password="",
                adhoc_password_env="",
                adhoc_password_file="",
            )
        )
        call = request.app.state.services.restore_service.start_restore

    request.app.state.services.repository_browser_service.resolve_backup_config_for_location.assert_not_called()
    call.assert_awaited_once_with(
        "demo",
        "local",
        "abcdef1234567890",
        mode="pattern",
        snapshot_paths=None,
        include_patterns=None,
        exclude_patterns=None,
        target=None,
        overwrite=False,
        resolved_backup=None,
    )


@pytest.mark.parametrize(
    "route",
    ["preview", "start"],
)
def test_restore_routes_no_donor_for_location_returns_controlled_error_fragment(
    route: str,
) -> None:
    from src.gui.routers.restore import preview_restore, start_restore

    request = _request()
    browser_service = request.app.state.services.repository_browser_service
    browser_service.resolve_backup_config_for_location.return_value = None

    if route == "preview":
        call = preview_restore(
            request,
            job_name="",
            backup_name="",
            location_id="loc-orphan",
            snapshot_id="abcdef1234567890",
            restore_mode="pattern",
            snapshot_paths="",
            include_patterns="",
            exclude_patterns="",
            target="",
            overwrite=False,
            adhoc_password="",
            adhoc_password_env="",
            adhoc_password_file="",
        )
    else:
        call = start_restore(
            request,
            job_name="",
            backup_name="",
            location_id="loc-orphan",
            snapshot_id="abcdef1234567890",
            restore_mode="pattern",
            snapshot_paths="",
            include_patterns="",
            exclude_patterns="",
            target="",
            overwrite=False,
            adhoc_password="",
            adhoc_password_env="",
            adhoc_password_file="",
        )

    response = asyncio.run(call)

    assert response.status_code == 200
    assert response.media_type == "text/html"
    assert not response.body.decode().lstrip().startswith("{")
    request.app.state.services.restore_service.preview_restore.assert_not_called()
    request.app.state.services.restore_service.start_restore.assert_not_called()


@pytest.mark.parametrize(
    "route",
    ["preview", "start"],
)
def test_restore_routes_use_adhoc_credentials_when_no_donor_exists(route: str) -> None:
    from src.gui.routers.restore import preview_restore, start_restore

    request = _request()
    browser_service = request.app.state.services.repository_browser_service
    browser_service.resolve_backup_config_for_location.return_value = None
    adhoc_override = MagicMock(name="adhoc_resolved_backup")
    browser_service.build_adhoc_backup_config.return_value = adhoc_override

    if route == "preview":
        request.app.state.services.restore_service.preview_restore = AsyncMock(
            return_value=MagicMock()
        )
        request.app.state.services.restore_service.preview_view.return_value = {"ok": True}
        asyncio.run(
            preview_restore(
                request,
                job_name="",
                backup_name="",
                location_id="loc-orphan",
                snapshot_id="abcdef1234567890",
                restore_mode="pattern",
                snapshot_paths="",
                include_patterns="",
                exclude_patterns="",
                target="",
                overwrite=False,
                adhoc_password="hunter2",
                adhoc_password_env="",
                adhoc_password_file="",
            )
        )
        call = request.app.state.services.restore_service.preview_restore
    else:
        request.app.state.services.restore_service.start_restore = AsyncMock(
            return_value=_restore_record("queued")
        )
        request.app.state.services.restore_service.restore_view.return_value = {"status": "queued"}
        asyncio.run(
            start_restore(
                request,
                job_name="",
                backup_name="",
                location_id="loc-orphan",
                snapshot_id="abcdef1234567890",
                restore_mode="pattern",
                snapshot_paths="",
                include_patterns="",
                exclude_patterns="",
                target="",
                overwrite=False,
                adhoc_password="hunter2",
                adhoc_password_env="",
                adhoc_password_file="",
            )
        )
        call = request.app.state.services.restore_service.start_restore

    browser_service.build_adhoc_backup_config.assert_called_once_with(
        "loc-orphan",
        password="hunter2",
        password_env=None,
        password_file=None,
    )
    call.assert_awaited_once_with(
        "__dockkeep_adhoc_restore__",
        "loc-orphan",
        "abcdef1234567890",
        mode="pattern",
        snapshot_paths=None,
        include_patterns=None,
        exclude_patterns=None,
        target=None,
        overwrite=False,
        resolved_backup=adhoc_override,
    )


@pytest.mark.parametrize(
    "route",
    ["preview", "start"],
)
def test_restore_routes_no_donor_no_adhoc_still_returns_controlled_error(route: str) -> None:
    from src.gui.routers.restore import preview_restore, start_restore

    request = _request()
    browser_service = request.app.state.services.repository_browser_service
    browser_service.resolve_backup_config_for_location.return_value = None

    if route == "preview":
        call = preview_restore(
            request,
            job_name="",
            backup_name="",
            location_id="loc-orphan",
            snapshot_id="abcdef1234567890",
            restore_mode="pattern",
            snapshot_paths="",
            include_patterns="",
            exclude_patterns="",
            target="",
            overwrite=False,
            adhoc_password="",
            adhoc_password_env="",
            adhoc_password_file="",
        )
    else:
        call = start_restore(
            request,
            job_name="",
            backup_name="",
            location_id="loc-orphan",
            snapshot_id="abcdef1234567890",
            restore_mode="pattern",
            snapshot_paths="",
            include_patterns="",
            exclude_patterns="",
            target="",
            overwrite=False,
            adhoc_password="",
            adhoc_password_env="",
            adhoc_password_file="",
        )

    response = asyncio.run(call)

    assert response.status_code == 200
    assert response.media_type == "text/html"
    assert not response.body.decode().lstrip().startswith("{")
    browser_service.build_adhoc_backup_config.assert_not_called()
    request.app.state.services.restore_service.preview_restore.assert_not_called()
    request.app.state.services.restore_service.start_restore.assert_not_called()


def test_restore_line_parser_preserves_commas() -> None:
    from src.gui.routers.restore import _parse_lines

    assert _parse_lines("/data/a,b.txt\n/etc") == ["/data/a,b.txt", "/etc"]
    assert _parse_lines("/data/a,b.txt, /etc") == ["/data/a,b.txt, /etc"]


def test_restore_router_has_no_operational_logic_or_json_response() -> None:
    source = (ROUTER_DIR / "restore.py").read_text(encoding="utf-8")

    forbidden = [
        "subprocess",
        "JSONResponse",
        "from pathlib",
        "import pathlib",
        ".mkdir(",
        ".open(",
        ".read_text(",
        ".write_text(",
        "os.",
        "shutil",
    ]
    for token in forbidden:
        assert token not in source


def test_jobs_router_does_not_expose_legacy_status_routes() -> None:
    from src.gui.routers.jobs import router

    paths = {route.path for route in router.routes}

    assert "/jobs/runs/{run_id}/status" not in paths
    assert "/jobs/{job_name}/{step}/status" not in paths
    assert "/jobs/{job_name}/{step}/dry-run-status" not in paths


def test_dashboard_renders_service_context() -> None:
    from src.gui.routers.dashboard import dashboard

    request = _request()
    rendered: list[dict[str, object]] = []

    def template_response(request: object, template: str, context: dict[str, object]) -> MagicMock:
        rendered.append({"template": template, "context": context})
        return MagicMock()

    request.app.state.templates.TemplateResponse.side_effect = template_response
    dashboard_view = {
        "overview": {},
        "jobs": [{"name": "demo"}],
        "config_error": None,
        "upcoming_runs": [],
        "active_runs": [],
        "recent_runs": [],
        "chart_data": {"growth": {"datasets": [{"label": "demo.local", "data": [1]}]}},
        "scheduler_available": True,
        "run_status_unknown": False,
        "degraded": False,
    }
    request.app.state.services.dashboard_service.get_dashboard_view.return_value = dashboard_view
    request.app.state.services.backup_stats_service.get_all_backup_stats.side_effect = (
        AssertionError("router composed dashboard stats")
    )
    request.app.state.services.backup_stats_service.get_dashboard_growth_data.side_effect = (
        AssertionError("router composed dashboard growth data")
    )
    asyncio.run(dashboard(request))

    request.app.state.services.dashboard_service.get_dashboard_view.assert_called_once_with()
    request.app.state.services.backup_stats_service.get_all_backup_stats.assert_not_called()
    request.app.state.services.backup_stats_service.get_dashboard_growth_data.assert_not_called()
    assert rendered[0]["template"] == "dashboard.html"
    assert rendered[0]["context"] == dashboard_view


def test_dashboard_job_status_fragment_uses_dashboard_service() -> None:
    from src.gui.routers.dashboard import dashboard_job_status

    request = _request()
    rendered: list[dict[str, object]] = []

    def template_response(
        request: object,
        template: str,
        context: dict[str, object],
        status_code: int = 200,
    ) -> MagicMock:
        rendered.append({"template": template, "context": context})
        return MagicMock(status_code=status_code, headers={})

    request.app.state.templates.TemplateResponse.side_effect = template_response
    request.app.state.services.dashboard_service.get_job_status_view.return_value = {
        "name": "demo",
        "is_running": True,
        "run_status_unknown": False,
    }

    asyncio.run(dashboard_job_status("demo", request))

    request.app.state.services.dashboard_service.get_job_status_view.assert_called_once_with("demo")
    request.app.state.services.dashboard_service.get_dashboard_view.assert_not_called()
    assert rendered == [
        {
            "template": "fragments/dashboard_job_status.html",
            "context": {"job": {"name": "demo", "is_running": True, "run_status_unknown": False}},
        }
    ]


def test_dashboard_runs_panel_fragment_uses_dashboard_service() -> None:
    from src.gui.routers.dashboard import dashboard_runs_panel

    request = _request()
    rendered: list[dict[str, object]] = []

    def template_response(
        request: object,
        template: str,
        context: dict[str, object],
        status_code: int = 200,
    ) -> MagicMock:
        rendered.append({"template": template, "context": context})
        return MagicMock(status_code=status_code, headers={})

    request.app.state.templates.TemplateResponse.side_effect = template_response
    panel_view = {"upcoming_runs": [], "active_runs": [], "recent_runs": []}
    request.app.state.services.dashboard_service.get_runs_panel_view.return_value = panel_view

    asyncio.run(dashboard_runs_panel(request))

    request.app.state.services.dashboard_service.get_runs_panel_view.assert_called_once_with()
    request.app.state.services.dashboard_service.get_dashboard_view.assert_not_called()
    assert rendered == [{"template": "fragments/dashboard_runs.html", "context": panel_view}]
