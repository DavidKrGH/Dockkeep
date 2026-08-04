import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.gui.app import create_app
from src.services.config_editor import ConfigEditorService

ROUTER_DIR = Path(__file__).resolve().parents[2] / "src" / "gui" / "routers"
TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "src" / "gui" / "templates"


def _make_config_editor_service(
    *,
    overview: dict | None = None,
    job_form: dict | None = None,
    save_job_form: dict | None = None,
    backup_defaults_form: dict | None = None,
    save_backup_defaults_form: dict | None = None,
    rclone_defaults_form: dict | None = None,
    save_rclone_defaults_form: dict | None = None,
    backup_form: dict | None = None,
    save_backup_form: dict | None = None,
    workflow_form: dict | None = None,
    save_workflow_form: dict | None = None,
    rclone_form: dict | None = None,
    save_rclone_form: dict | None = None,
    delete_rclone: dict | None = None,
    test_notification_provider: dict | None = None,
    test_notification_report: dict | None = None,
) -> MagicMock:
    svc = MagicMock(spec=ConfigEditorService)
    svc.get_overview = AsyncMock(return_value=overview or {"jobs": [], "error": None})
    svc.get_global_form = AsyncMock(return_value={"groups": [], "providers": [], "error": None})
    svc.save_global_form = AsyncMock(return_value={"saved": False, "error": "mock"})
    svc.get_job_form = AsyncMock(
        return_value=job_form
        or {
            "name": "",
            "sources": [],
            "password_env": "",
            "error": None,
        }
    )
    svc.save_job_form = AsyncMock(return_value=save_job_form or {"saved": False, "error": "mock"})
    svc.get_backup_defaults_form = AsyncMock(
        return_value=backup_defaults_form
        or {
            "credential": {},
            "groups": [],
            "error": None,
        }
    )
    svc.save_backup_defaults_form = AsyncMock(
        return_value=save_backup_defaults_form or {"saved": False, "error": "mock"}
    )
    svc.get_rclone_defaults_form = AsyncMock(
        return_value=rclone_defaults_form
        or {
            "groups": [],
            "error": None,
        }
    )
    svc.save_rclone_defaults_form = AsyncMock(
        return_value=save_rclone_defaults_form or {"saved": False, "error": "mock"}
    )
    svc.get_backup_form = AsyncMock(
        return_value=backup_form
        or {
            "name": "",
            "repository": "",
            "credential": {},
            "groups": [],
            "error": None,
        }
    )
    svc.save_backup_form = AsyncMock(
        return_value=save_backup_form or {"saved": False, "error": "mock"}
    )
    svc.get_workflow_form = AsyncMock(
        return_value=workflow_form
        or {
            "name": "",
            "schedule": "",
            "steps": [],
            "step_rows": [],
            "available_steps": [],
            "available_backups": [],
            "available_rclone_tasks": [],
            "error": None,
        }
    )
    svc.save_workflow_form = AsyncMock(
        return_value=save_workflow_form or {"saved": False, "error": "mock"}
    )
    svc.get_rclone_form = AsyncMock(
        return_value=rclone_form
        or {
            "name": "",
            "source": "",
            "target": "",
            "sync_delete": False,
            "primary_fields": [],
            "groups": [],
            "error": None,
        }
    )
    svc.save_rclone_form = AsyncMock(
        return_value=save_rclone_form or {"saved": False, "error": "mock"}
    )
    svc.delete_job = AsyncMock(return_value={"saved": False, "error": "mock"})
    svc.delete_backup = AsyncMock(return_value={"saved": False, "error": "mock"})
    svc.delete_workflow = AsyncMock(return_value={"saved": False, "error": "mock"})
    svc.delete_rclone = AsyncMock(return_value=delete_rclone or {"saved": False, "error": "mock"})
    svc.test_notification_provider = AsyncMock(
        return_value=test_notification_provider
        or {
            "ok": True,
            "provider": "mail",
            "message": "Test notification sent successfully.",
        }
    )
    svc.test_notification_report = AsyncMock(
        return_value=test_notification_report
        or {
            "ok": True,
            "provider": "mail",
            "message": "Test report sent successfully.",
        }
    )
    return svc


def _request(
    config_editor_service: MagicMock | None = None, *, headers: dict[str, str] | None = None
) -> MagicMock:
    request = MagicMock()
    request.query_params = {}
    request.headers = headers or {}
    services = SimpleNamespace(
        config_service=MagicMock(),
        run_manager=MagicMock(),
        run_service=MagicMock(),
        log_service=MagicMock(),
        rclone_service=MagicMock(),
        repository_service=MagicMock(),
        repository_browser_service=MagicMock(),
        restore_registry=MagicMock(),
        restore_service=MagicMock(),
        dashboard_service=MagicMock(),
        config_editor_service=config_editor_service or _make_config_editor_service(),
    )
    request.app.state.services = services

    def template_response(
        _req: object,
        template: str,
        context: dict,
        status_code: int = 200,
    ) -> HTMLResponse:
        body = f"{template} {sorted(context.keys())}"
        return HTMLResponse(body, status_code=status_code)

    request.app.state.templates.TemplateResponse.side_effect = template_response
    return request


def _set_schema_form(request: MagicMock, values: dict[str, str]) -> None:
    request.form = AsyncMock(return_value={"_schema_form": "1", **values})


def test_overview_renders_job_list() -> None:
    from src.gui.routers.config_editor import editor_overview

    svc = _make_config_editor_service(
        overview={
            "jobs": [
                {
                    "name": "myjob",
                    "backup_names": ["local"],
                    "workflow_names": [],
                    "rclone_names": [],
                },
                {
                    "name": "other",
                    "backup_names": [],
                    "workflow_names": ["nightly"],
                    "rclone_names": ["offsite"],
                },
            ],
            "error": None,
            "warnings": ["Notifications are enabled, but no provider is configured."],
        }
    )
    request = _request(svc)
    asyncio.run(editor_overview(request))

    svc.get_overview.assert_called_once_with()
    _, template, context = request.app.state.templates.TemplateResponse.call_args.args
    assert template == "config_editor_overview.html"
    assert context["overview"]["warnings"] == [
        "Notifications are enabled, but no provider is configured."
    ]


def test_job_sidebar_orders_editable_tasks_before_workflows() -> None:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.from_string(
        "{% from '_config_editor_nav.html' import job_sidebar %}"
        "{{ job_sidebar(job_nav, 'settings') }}"
    )
    job_nav = SimpleNamespace(
        name="demo",
        name_valid=True,
        backup_names=["local"],
        backup_name_valid={"local": True},
        rclone_names=["offsite"],
        rclone_name_valid={"offsite": True},
        workflow_names=["nightly"],
        workflow_name_valid={"nightly": True},
    )

    rendered = template.render(job_nav=job_nav)

    assert rendered.index("Backups") < rendered.index("Rclone tasks")
    assert rendered.index("Rclone tasks") < rendered.index("Workflows")


def _parse_options(rendered: str) -> dict[str, bool]:
    return {
        match.group(1): "selected" in match.group(2)
        for match in re.finditer(r'<option value="([^"]*)"([^>]*)>', rendered)
    }


def test_job_sidebar_renders_job_switcher_for_valid_jobs() -> None:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.from_string(
        "{% from '_config_editor_nav.html' import job_sidebar %}"
        "{{ job_sidebar(job_nav, 'settings', none, jobs) }}"
    )
    job_nav = SimpleNamespace(
        name="demo",
        name_valid=True,
        backup_names=[],
        backup_name_valid={},
        rclone_names=[],
        rclone_name_valid={},
        workflow_names=[],
        workflow_name_valid={},
    )
    jobs = [
        {"name": "demo", "name_valid": True},
        {"name": "archive", "name_valid": True},
        {"name": "bad.name", "name_valid": False},
    ]

    options = _parse_options(template.render(job_nav=job_nav, jobs=jobs))

    assert options == {
        "/config/jobs/demo/effective": True,
        "/config/jobs/archive/effective": False,
    }


def test_job_sidebar_omits_switcher_when_only_one_job_is_reachable() -> None:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.from_string(
        "{% from '_config_editor_nav.html' import job_sidebar %}"
        "{{ job_sidebar(job_nav, 'settings', none, jobs) }}"
    )
    job_nav = SimpleNamespace(
        name="demo",
        name_valid=True,
        backup_names=[],
        backup_name_valid={},
        rclone_names=[],
        rclone_name_valid={},
        workflow_names=[],
        workflow_name_valid={},
    )
    jobs = [
        {"name": "demo", "name_valid": True},
        {"name": "bad.name", "name_valid": False},
    ]

    rendered = template.render(job_nav=job_nav, jobs=jobs)

    assert _parse_options(rendered) == {}
    assert "demo" in rendered


def test_test_notification_provider_route_delegates_to_service() -> None:
    from src.gui.routers.config_editor import test_notification_provider

    service = _make_config_editor_service(
        test_notification_provider={
            "ok": False,
            "provider": "mail",
            "message": "Test notification failed: missing credentials",
        }
    )
    request = _request(service)
    _set_schema_form(request, {"mail__enabled": "true", "mail__host": "smtp.example.com"})

    asyncio.run(test_notification_provider("mail", request))

    service.test_notification_provider.assert_called_once_with(
        "mail",
        {
            "_schema_form": "1",
            "mail__enabled": "true",
            "mail__host": "smtp.example.com",
        },
    )
    _, template, context = request.app.state.templates.TemplateResponse.call_args.args
    assert template == "fragments/notification_provider_test.html"
    assert context["result"]["message"] == "Test notification failed: missing credentials"


def test_test_notification_report_route_delegates_to_service() -> None:
    from src.gui.routers.config_editor import test_notification_report

    service = _make_config_editor_service(
        test_notification_report={
            "ok": False,
            "provider": "mail",
            "message": "Test report failed: missing credentials",
        }
    )
    request = _request(service)
    _set_schema_form(request, {"mail__enabled": "true", "mail__host": "smtp.example.com"})

    asyncio.run(test_notification_report("mail", request))

    service.test_notification_report.assert_called_once_with(
        "mail",
        {
            "_schema_form": "1",
            "mail__enabled": "true",
            "mail__host": "smtp.example.com",
        },
    )
    _, template, context = request.app.state.templates.TemplateResponse.call_args.args
    assert template == "fragments/notification_provider_test.html"
    assert context["result"]["message"] == "Test report failed: missing credentials"


@pytest.mark.parametrize("field_set_name", ["backup", "workflow", "rclone"])
def test_schedule_fields_keep_cron_schema_contract(field_set_name: str) -> None:
    from src.services.config_editor_schema import (
        BACKUP_FIELDS,
        RCLONE_FIELDS,
        WORKFLOW_FIELDS,
        field_views,
    )

    fields_by_name = {
        "backup": BACKUP_FIELDS,
        "workflow": WORKFLOW_FIELDS,
        "rclone": RCLONE_FIELDS,
    }
    schedule = next(
        field
        for field in field_views({}, fields_by_name[field_set_name])
        if field["name"] == "schedule"
    )

    assert schedule["label"] == "Cron schedule"
    assert schedule["kind"] == "text"
    assert schedule["cron"] is True
    assert schedule["hint"] == "0 2 * * *"


def test_report_schedule_keeps_cron_schema_contract() -> None:
    from src.services.config_editor_schema import GLOBAL_NOTIFICATION_FIELDS, field_views

    report_schedule = next(
        field
        for field in field_views({}, GLOBAL_NOTIFICATION_FIELDS, prefix="notifications__")
        if field["name"] == "notifications__report_schedule"
    )

    assert report_schedule["label"] == "Report schedule"
    assert report_schedule["kind"] == "text"
    assert report_schedule["cron"] is True
    assert report_schedule["hint"] == "0 8 * * *"


def test_filter_fields_expose_backend_info_in_schema_viewmodels() -> None:
    from src.services.config_editor_schema import BACKUP_FIELDS, RCLONE_FIELDS, field_views

    backup_exclude = next(
        field for field in field_views({}, BACKUP_FIELDS) if field["name"] == "exclude"
    )
    rclone_exclude = next(
        field for field in field_views({}, RCLONE_FIELDS) if field["name"] == "exclude"
    )

    assert {panel["backend"] for panel in backup_exclude["info_panels"]} == {
        "restic",
        "rclone",
    }
    assert {panel["backend"] for panel in rclone_exclude["info_panels"]} == {"rclone"}


@pytest.mark.parametrize(
    "path",
    [
        "/config/jobs/missing",
        "/config/jobs/demo/backups/missing",
        "/config/jobs/demo/workflows/missing",
        "/config/jobs/demo/rclone/missing",
        "/config/jobs/demo/backups/local",
    ],
)
def test_missing_named_editor_resources_render_the_404_page(tmp_path: Path, path: str) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[jobs.demo]\n\n"
        "[jobs.demo.backup.local]\n"
        'repository = "/tmp/dockkeep-test-repo"\n'
        'password = "test"\n'
        'sources = ["/etc"]\n',
        encoding="utf-8",
    )
    expected_status = 200 if path.endswith("/local") else 404

    with patch("src.gui.app.SchedulerOwnerManager", return_value=MagicMock()):
        app = create_app(config_path, appdata_dir=tmp_path)
        with TestClient(app) as client:
            response = client.get(path)

    assert response.status_code == expected_status
    assert "<!DOCTYPE html>" in response.text
    if expected_status == 404:
        assert "404 Not Found" in response.text


def test_config_editor_routes_reject_invalid_resource_names() -> None:
    from src.gui.routers.config_editor import router as config_editor_router

    app = FastAPI()
    app.include_router(config_editor_router)
    with TestClient(app) as client:
        response = client.get("/config/jobs/not.valid")

    assert response.status_code == 422


def test_new_job_page_is_not_exposed_as_fallback() -> None:
    from src.gui.routers.config_editor import router as config_editor_router

    app = FastAPI()
    app.include_router(config_editor_router)
    with TestClient(app) as client:
        response = client.get("/config/jobs/new")

    assert response.status_code == 404


def test_edit_job_page_renders_populated_form() -> None:
    from src.gui.routers.config_editor import edit_job_page

    svc = _make_config_editor_service(
        overview={
            "jobs": [
                {
                    "name": "myjob",
                    "name_valid": True,
                    "backup_names": [],
                    "backup_name_valid": {},
                    "workflow_names": [],
                    "workflow_name_valid": {},
                    "rclone_names": [],
                    "rclone_name_valid": {},
                },
                {
                    "name": "other",
                    "name_valid": True,
                    "backup_names": [],
                    "backup_name_valid": {},
                    "workflow_names": [],
                    "workflow_name_valid": {},
                    "rclone_names": [],
                    "rclone_name_valid": {},
                },
            ],
            "error": None,
        },
        job_form={"name": "myjob", "sources": ["/data"], "password_env": "PASS", "error": None},
    )
    request = _request(svc)
    asyncio.run(edit_job_page("myjob", request))

    svc.get_job_form.assert_called_once_with("myjob")
    _, _, context = request.app.state.templates.TemplateResponse.call_args.args
    assert [job["name"] for job in context["job_nav_jobs"]] == ["myjob", "other"]


def test_create_job_success_redirects_to_job_settings() -> None:
    from src.gui.routers.config_editor import create_job

    svc = _make_config_editor_service(save_job_form={"saved": True, "error": None})
    request = _request(svc)
    _set_schema_form(request, {"name": "newjob"})
    response = asyncio.run(create_job(request))

    svc.save_job_form.assert_called_once_with(
        job_name=None,
        form={
            "_schema_form": "1",
            "name": "newjob",
        },
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/config/jobs/newjob?saved=1"


def test_create_job_error_rerenders_form() -> None:
    from src.gui.routers.config_editor import create_job

    svc = _make_config_editor_service(save_job_form={"saved": False, "error": "Name invalid"})
    request = _request(svc)
    _set_schema_form(request, {"name": "bad name", "sources": ""})
    response = asyncio.run(create_job(request))

    assert response.status_code == 200
    _, template, context = request.app.state.templates.TemplateResponse.call_args.args
    assert template == "config_editor_overview.html"
    assert context["new_job_modal_open"] is True
    assert context["new_job_form"] == {"name": "bad name", "error": "Name invalid"}


def test_update_job_success_redirects_back_to_form() -> None:
    from src.gui.routers.config_editor import update_job

    svc = _make_config_editor_service(save_job_form={"saved": True, "error": None})
    request = _request(svc)
    _set_schema_form(
        request, {"name": "myjob", "sources": "/data\n/etc", "credential__password_env": "P"}
    )
    response = asyncio.run(update_job("myjob", request))

    svc.save_job_form.assert_called_once_with(
        job_name="myjob",
        form={
            "_schema_form": "1",
            "name": "myjob",
            "sources": "/data\n/etc",
            "credential__password_env": "P",
        },
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/config/jobs/myjob?saved=1"


def test_update_job_error_rerenders_form() -> None:
    from src.gui.routers.config_editor import update_job

    svc = _make_config_editor_service(save_job_form={"saved": False, "error": "Job not found"})
    request = _request(svc)
    _set_schema_form(request, {"name": "missing", "sources": ""})
    response = asyncio.run(update_job("missing", request))

    assert response.status_code == 200


def test_job_editor_does_not_render_job_delete_action() -> None:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
    )
    env.globals["url_for"] = lambda name, path: f"/static{path}"

    html = env.get_template("config_editor_job.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/config/jobs/demo")),
        form_data={
            "error": None,
            "name": "demo",
            "credential": {
                "mode": "inherit",
                "modes": [("inherit", "Inherit")],
                "parent_configured": False,
                "password_configured": False,
            },
            "groups": [],
        },
        job_name="demo",
        is_new=False,
    )

    assert "Delete job" not in html


def test_schema_based_job_post_uses_form_service() -> None:
    from src.gui.routers.config_editor import update_job

    svc = _make_config_editor_service()
    svc.save_job_form.return_value = {"saved": True, "error": None}
    request = _request(svc)
    _set_schema_form(
        request, {"name": "demo", "sources": "/data\n/etc", "credential__mode": "inherit"}
    )

    response = asyncio.run(update_job("demo", request))

    svc.save_job_form.assert_called_once_with(
        job_name="demo",
        form={
            "_schema_form": "1",
            "name": "demo",
            "sources": "/data\n/etc",
            "credential__mode": "inherit",
        },
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/config/jobs/demo?saved=1"


def test_update_backup_defaults_success_redirects_back_to_form() -> None:
    from src.gui.routers.config_editor import update_backup_defaults

    svc = _make_config_editor_service(save_backup_defaults_form={"saved": True, "error": None})
    request = _request(svc)
    form = {"backup_timeout": "60", "credential__mode": "inherit"}
    _set_schema_form(request, form)

    response = asyncio.run(update_backup_defaults("myjob", request))

    svc.save_backup_defaults_form.assert_called_once_with(
        job_name="myjob",
        form={"_schema_form": "1", **form},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/config/jobs/myjob/backup-defaults?saved=1"


def test_update_rclone_defaults_success_redirects_back_to_form() -> None:
    from src.gui.routers.config_editor import update_rclone_defaults

    svc = _make_config_editor_service(save_rclone_defaults_form={"saved": True, "error": None})
    request = _request(svc)
    form = {"transfers": "4"}
    _set_schema_form(request, form)

    response = asyncio.run(update_rclone_defaults("myjob", request))

    svc.save_rclone_defaults_form.assert_called_once_with(
        job_name="myjob",
        form={"_schema_form": "1", **form},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/config/jobs/myjob/rclone-defaults?saved=1"


def test_new_backup_page_renders_backup_form() -> None:
    from src.gui.routers.config_editor import new_backup_page

    svc = _make_config_editor_service()
    request = _request(svc)
    asyncio.run(new_backup_page("myjob", request))

    svc.get_backup_form.assert_called_once_with("myjob")


def test_create_backup_success_redirects() -> None:
    from src.gui.routers.config_editor import create_backup

    svc = _make_config_editor_service(save_backup_form={"saved": True, "error": None})
    request = _request(svc)
    form = {
        "name": "local",
        "repository": "/backup",
        "sources": "/data",
        "schedule": "0 2 * * *",
        "credential__password_env": "PASS",
        "keep_last": "5",
    }
    _set_schema_form(request, form)
    response = asyncio.run(create_backup("myjob", request))

    svc.save_backup_form.assert_called_once_with(
        job_name="myjob",
        backup_name=None,
        form={"_schema_form": "1", **form},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/config/jobs/myjob/backups/local?saved=1"


def test_create_backup_error_rerenders_form() -> None:
    from src.gui.routers.config_editor import create_backup

    svc = _make_config_editor_service(
        save_backup_form={"saved": False, "error": "Repository required"}
    )
    request = _request(svc)
    _set_schema_form(request, {"name": "bad", "repository": ""})
    response = asyncio.run(create_backup("myjob", request))

    assert response.status_code == 200


def test_edit_backup_page_renders_backup_form() -> None:
    from src.gui.routers.config_editor import edit_backup_page

    svc = _make_config_editor_service(
        backup_form={
            "name": "local",
            "repository": "/backup",
            "credential": {},
            "groups": [],
            "error": None,
        }
    )
    request = _request(svc)
    asyncio.run(edit_backup_page("myjob", "local", request))

    svc.get_backup_form.assert_called_once_with("myjob", "local")


def test_update_backup_success_redirects_back_to_form() -> None:
    from src.gui.routers.config_editor import update_backup

    svc = _make_config_editor_service(save_backup_form={"saved": True, "error": None})
    request = _request(svc)
    form = {
        "name": "local",
        "repository": "/backup",
        "sources": "/data",
        "credential__password_env": "PASS",
    }
    _set_schema_form(request, form)
    response = asyncio.run(update_backup("myjob", "local", request))

    svc.save_backup_form.assert_called_once_with(
        job_name="myjob",
        backup_name="local",
        form={"_schema_form": "1", **form},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/config/jobs/myjob/backups/local?saved=1"


def test_new_workflow_page_renders_workflow_form() -> None:
    from src.gui.routers.config_editor import new_workflow_page

    svc = _make_config_editor_service(
        workflow_form={
            "name": "",
            "schedule": "",
            "steps": [],
            "step_rows": [],
            "available_steps": ["backup.local", "backup.archive", "rclone.offsite"],
            "available_backups": ["local", "archive"],
            "available_rclone_tasks": ["offsite"],
            "error": None,
        }
    )
    request = _request(svc)
    asyncio.run(new_workflow_page("myjob", request))

    svc.get_workflow_form.assert_called_once_with("myjob")


def test_create_workflow_success_redirects() -> None:
    from src.gui.routers.config_editor import create_workflow

    svc = _make_config_editor_service(save_workflow_form={"saved": True, "error": None})
    request = _request(svc)
    form = {"name": "nightly", "schedule": "0 3 * * *", "steps": "backup.local"}
    _set_schema_form(request, form)
    response = asyncio.run(create_workflow("myjob", request))

    svc.save_workflow_form.assert_called_once_with(
        job_name="myjob",
        workflow_name=None,
        form={"_schema_form": "1", **form},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/config/jobs/myjob/workflows/nightly?saved=1"


def test_create_workflow_error_rerenders_form() -> None:
    from src.gui.routers.config_editor import create_workflow

    svc = _make_config_editor_service(save_workflow_form={"saved": False, "error": "No steps"})
    request = _request(svc)
    _set_schema_form(request, {"name": "bad", "schedule": "", "steps": ""})
    response = asyncio.run(create_workflow("myjob", request))

    assert response.status_code == 200


def test_edit_workflow_page_renders_workflow_form() -> None:
    from src.gui.routers.config_editor import edit_workflow_page

    svc = _make_config_editor_service(
        workflow_form={
            "name": "nightly",
            "schedule": "0 3 * * *",
            "steps": ["backup.local"],
            "step_rows": [{"kind": "backup", "task": "local", "action": "all"}],
            "available_steps": ["backup.local", "rclone.offsite"],
            "available_backups": ["local"],
            "available_rclone_tasks": ["offsite"],
            "error": None,
        }
    )
    request = _request(svc)
    asyncio.run(edit_workflow_page("myjob", "nightly", request))

    svc.get_workflow_form.assert_called_once_with("myjob", "nightly")


def test_update_workflow_success_redirects_back_to_form() -> None:
    from src.gui.routers.config_editor import update_workflow

    svc = _make_config_editor_service(save_workflow_form={"saved": True, "error": None})
    request = _request(svc)
    form = {"name": "nightly", "schedule": "0 3 * * *", "steps": "backup.local"}
    _set_schema_form(request, form)
    response = asyncio.run(update_workflow("myjob", "nightly", request))

    svc.save_workflow_form.assert_called_once_with(
        job_name="myjob",
        workflow_name="nightly",
        form={"_schema_form": "1", **form},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/config/jobs/myjob/workflows/nightly?saved=1"


def test_rclone_page_renders_rclone_form() -> None:
    from src.gui.routers.config_editor import edit_rclone_page

    svc = _make_config_editor_service(
        rclone_form={
            "name": "default",
            "source": "/backup",
            "target": "s3:bucket",
            "sync_delete": True,
            "primary_fields": [
                {
                    "name": "source",
                    "label": "Source path",
                    "value": "/backup",
                    "hint": "",
                    "required": True,
                },
                {
                    "name": "target",
                    "label": "Rclone target",
                    "value": "s3:bucket",
                    "hint": "myremote:bucket/path",
                    "required": True,
                },
            ],
            "groups": [],
            "error": None,
        }
    )
    request = _request(svc)
    asyncio.run(edit_rclone_page("myjob", "default", request))

    svc.get_rclone_form.assert_called_once_with("myjob", "default")


def test_create_rclone_schema_form_uses_form_service() -> None:
    from src.gui.routers.config_editor import create_rclone

    svc = _make_config_editor_service(save_rclone_form={"saved": True, "error": None})
    request = _request(svc)
    _set_schema_form(request, {"name": "archive", "source": "/backup", "target": "s3:bucket"})

    response = asyncio.run(create_rclone("myjob", request))

    svc.save_rclone_form.assert_called_once_with(
        job_name="myjob",
        rclone_name=None,
        form={
            "_schema_form": "1",
            "name": "archive",
            "source": "/backup",
            "target": "s3:bucket",
        },
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/config/jobs/myjob/rclone/archive?saved=1"


def test_update_rclone_success_redirects() -> None:
    from src.gui.routers.config_editor import update_rclone

    svc = _make_config_editor_service(save_rclone_form={"saved": True, "error": None})
    request = _request(svc)
    form = {"name": "default", "source": "/backup", "target": "s3:bucket", "sync_delete": "true"}
    _set_schema_form(request, form)
    response = asyncio.run(update_rclone("myjob", "default", request))

    svc.save_rclone_form.assert_called_once_with(
        job_name="myjob",
        rclone_name="default",
        form={"_schema_form": "1", **form},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/config/jobs/myjob/rclone/default?saved=1"


def test_update_rclone_error_rerenders_form() -> None:
    from src.gui.routers.config_editor import update_rclone

    svc = _make_config_editor_service(save_rclone_form={"saved": False, "error": "Source empty"})
    request = _request(svc)
    _set_schema_form(request, {"name": "default", "source": "", "target": "s3:bucket"})
    response = asyncio.run(update_rclone("myjob", "default", request))

    assert response.status_code == 200


def test_update_rclone_unchecked_sync_delete_omits_bool_field() -> None:
    from src.gui.routers.config_editor import update_rclone

    svc = _make_config_editor_service(save_rclone_form={"saved": True, "error": None})
    request = _request(svc)
    form = {"name": "default", "source": "/backup", "target": "s3:bucket"}
    _set_schema_form(request, form)
    asyncio.run(update_rclone("myjob", "default", request))

    svc.save_rclone_form.assert_called_once_with(
        job_name="myjob",
        rclone_name="default",
        form={"_schema_form": "1", **form},
    )


def test_config_editor_router_has_no_operational_logic_or_json_response() -> None:
    source = (ROUTER_DIR / "config_editor.py").read_text(encoding="utf-8")

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
        assert token not in source, f"Forbidden token {token!r} found in config_editor.py"


@pytest.mark.parametrize(
    ("route_name", "kwargs", "expected_location"),
    [
        ("delete_job", {"job_name": "myjob"}, "/config/jobs"),
        (
            "delete_backup",
            {"job_name": "myjob", "backup_name": "local"},
            "/config/jobs/myjob",
        ),
    ],
)
def test_delete_success_uses_hx_redirect_for_htmx_requests(
    route_name: str, kwargs: dict[str, str], expected_location: str
) -> None:
    import src.gui.routers.config_editor as config_editor

    svc = _make_config_editor_service()
    svc.delete_job = AsyncMock(return_value={"saved": True, "error": None})
    svc.delete_backup = AsyncMock(return_value={"saved": True, "error": None})
    request = _request(svc, headers={"HX-Request": "true"})
    response = asyncio.run(getattr(config_editor, route_name)(request=request, **kwargs))

    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == expected_location
    assert "location" not in response.headers


@pytest.mark.parametrize(
    ("route_name", "kwargs", "expected_location"),
    [
        ("delete_job", {"job_name": "myjob"}, "/config/jobs"),
        (
            "delete_backup",
            {"job_name": "myjob", "backup_name": "local"},
            "/config/jobs/myjob",
        ),
    ],
)
def test_delete_success_uses_303_redirect_for_non_htmx_requests(
    route_name: str, kwargs: dict[str, str], expected_location: str
) -> None:
    import src.gui.routers.config_editor as config_editor

    svc = _make_config_editor_service()
    svc.delete_job = AsyncMock(return_value={"saved": True, "error": None})
    svc.delete_backup = AsyncMock(return_value={"saved": True, "error": None})
    request = _request(svc)
    response = asyncio.run(getattr(config_editor, route_name)(request=request, **kwargs))

    assert response.status_code == 303
    assert response.headers["location"] == expected_location
    assert "HX-Redirect" not in response.headers


def test_config_editor_templates_use_unified_delete_confirmation() -> None:
    template_names = [
        "config_editor_backup.html",
        "config_editor_overview.html",
        "config_editor_rclone.html",
        "config_editor_workflow.html",
    ]

    for template_name in template_names:
        source = (TEMPLATE_DIR / template_name).read_text(encoding="utf-8")
        assert 'onsubmit="return confirm' not in source
        assert "hx-confirm=" in source
        assert "hx-post=" in source


def test_sources_multiline_textarea_passed_to_schema_service() -> None:
    from src.gui.routers.config_editor import create_job

    svc = _make_config_editor_service(save_job_form={"saved": True, "error": None})
    request = _request(svc)
    _set_schema_form(request, {"name": "myjob", "sources": "  /data  \n\n  /backup  \n  "})
    asyncio.run(create_job(request))

    svc.save_job_form.assert_called_once_with(
        job_name=None,
        form={
            "_schema_form": "1",
            "name": "myjob",
            "sources": "  /data  \n\n  /backup  \n  ",
        },
    )


def test_steps_multiline_textarea_passed_to_schema_service() -> None:
    from src.gui.routers.config_editor import create_workflow

    svc = _make_config_editor_service(save_workflow_form={"saved": True, "error": None})
    request = _request(svc)
    _set_schema_form(
        request,
        {"name": "nightly", "schedule": "", "steps": "  backup.local  \n\n  backup.archive  \n"},
    )
    asyncio.run(create_workflow("myjob", request))

    svc.save_workflow_form.assert_called_once_with(
        job_name="myjob",
        workflow_name=None,
        form={
            "_schema_form": "1",
            "name": "nightly",
            "schedule": "",
            "steps": "  backup.local  \n\n  backup.archive  \n",
        },
    )


def _effective_view() -> dict:
    return {
        "job_name": "myjob",
        "job_groups": [
            {
                "label": "Timeouts",
                "fields": [{"label": "Hook timeout (seconds)", "value": "200", "source": "job"}],
            }
        ],
        "backups": [
            {
                "name": "local",
                "summary_fields": [
                    {"label": "Repository", "value": "/repo", "source": "task", "kind": "path"},
                    {
                        "label": "Cron schedule",
                        "value": "0 3 * * *",
                        "source": "task",
                        "note": "next run 2026-07-17 03:00",
                    },
                ],
                "main_group": {
                    "label": "Input",
                    "fields": [
                        {
                            "label": "Source paths",
                            "value": "/data",
                            "list_items": ["/data"],
                            "source": "task",
                        }
                    ],
                },
                "steps": [],
                "groups": [
                    {
                        "label": "Retention",
                        "fields": [
                            {"label": "Keep last N", "value": "3", "source": "global"},
                            {
                                "label": "Run retention automatically",
                                "value": "no",
                                "source": "default",
                            },
                        ],
                    }
                ],
                "warnings": ["No backup inputs configured."],
            }
        ],
        "workflows": [
            {
                "name": "nightly",
                "summary_fields": [
                    {"label": "Cron schedule", "value": "manual only", "source": "default"}
                ],
                "main_group": None,
                "steps": [{"label": "backup.local", "kind": "backup", "target": "local"}],
                "groups": [],
                "warnings": [],
            }
        ],
        "rclone_tasks": [
            {
                "name": "mirror",
                "summary_fields": [
                    {"label": "Source", "value": "/repo", "source": "task", "kind": "path"},
                    {
                        "label": "Target",
                        "value": "remote:bucket/repo",
                        "source": "task",
                        "kind": "path",
                    },
                ],
                "main_group": None,
                "steps": [],
                "groups": [],
                "warnings": [],
            }
        ],
    }


def test_effective_job_page_delegates_to_config_service() -> None:
    from src.gui.routers.config_editor import effective_job_page

    svc = _make_config_editor_service(
        overview={
            "jobs": [
                {
                    "name": "myjob",
                    "name_valid": True,
                    "backup_names": ["local"],
                    "backup_name_valid": {"local": True},
                    "workflow_names": [],
                    "workflow_name_valid": {},
                    "rclone_names": [],
                    "rclone_name_valid": {},
                },
                {
                    "name": "other",
                    "name_valid": True,
                    "backup_names": [],
                    "backup_name_valid": {},
                    "workflow_names": [],
                    "workflow_name_valid": {},
                    "rclone_names": [],
                    "rclone_name_valid": {},
                },
            ],
            "error": None,
        }
    )
    request = _request(svc)
    request.app.state.services.config_service.get_effective_job_view = AsyncMock(
        return_value=_effective_view()
    )

    asyncio.run(effective_job_page("myjob", request))

    request.app.state.services.config_service.get_effective_job_view.assert_called_once_with(
        "myjob"
    )
    _, template, context = request.app.state.templates.TemplateResponse.call_args.args
    assert template == "config_editor_effective.html"
    assert context["job_nav_active"] == "effective"
    assert context["job_nav"]["name"] == "myjob"
    assert [job["name"] for job in context["job_nav_jobs"]] == ["myjob", "other"]
    assert context["view"]["job_name"] == "myjob"


def test_effective_template_renders_resolved_values() -> None:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
    )
    env.globals["url_for"] = lambda name, path: f"/static{path}"

    html = env.get_template("config_editor_effective.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/config/jobs/myjob/effective")),
        view=_effective_view(),
        job_nav=None,
        job_nav_active="effective",
        job_nav_resource=None,
    )

    assert "Keep last N" in html
    assert "/repo" in html
    assert "effective-source-default" in html
    assert 'href="#backup-local"' in html

    assert html.index("Job level") < html.index('id="backup-local"')
    assert html.index('id="backup-local"') < html.index('id="rclone-mirror"')
    assert html.index('id="rclone-mirror"') < html.index('id="workflow-nightly"')
