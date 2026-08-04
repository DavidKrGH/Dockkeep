from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from ._config_editor_crud import (
    BACKUP_DEFAULTS_FORM,
    BACKUP_FORM,
    JOB_FORM,
    RCLONE_DEFAULTS_FORM,
    RCLONE_FORM,
    WORKFLOW_FORM,
    delete_resource_form,
    redirect,
    render_resource_form,
    save_resource_form,
)
from ._helpers import ResourceName, fragment_response, get_services, template_response

router = APIRouter(prefix="/config")


async def _schema_form(request: Request) -> dict[str, str]:
    values = await request.form()
    return {str(key): str(value) for key, value in values.items()}


@router.get("/jobs", response_class=HTMLResponse)
async def editor_overview(request: Request) -> HTMLResponse:
    service = get_services(request).config_editor_service
    overview = await service.get_overview()
    global_form = await service.get_global_form()
    global_saved = request.query_params.get("saved") == "global"
    return template_response(
        request,
        "config_editor_overview.html",
        {
            "overview": overview,
            "global_form": global_form,
            "global_saved": global_saved,
            "global_open": global_saved,
        },
    )


@router.post("/global", response_class=HTMLResponse)
async def update_global(request: Request) -> Response:
    service = get_services(request).config_editor_service
    submitted = await _schema_form(request)
    result = await service.save_global_form(submitted)
    if result.get("saved"):
        return redirect("/config/jobs?saved=global")
    overview = await service.get_overview()
    global_form = await service.get_global_form(submitted=submitted)
    global_form["error"] = result.get("error")
    return template_response(
        request,
        "config_editor_overview.html",
        {
            "overview": overview,
            "global_form": global_form,
            "global_saved": False,
            "global_open": True,
        },
    )


@router.post("/global/providers/{provider}/test", response_class=HTMLResponse)
async def test_notification_provider(provider: ResourceName, request: Request) -> HTMLResponse:
    service = get_services(request).config_editor_service
    submitted = await _schema_form(request)
    result = await service.test_notification_provider(provider, submitted)
    return fragment_response(
        request,
        "fragments/notification_provider_test.html",
        {"result": result},
    )


@router.post("/global/providers/{provider}/test-report", response_class=HTMLResponse)
async def test_notification_report(provider: ResourceName, request: Request) -> HTMLResponse:
    service = get_services(request).config_editor_service
    submitted = await _schema_form(request)
    result = await service.test_notification_report(provider, submitted)
    return fragment_response(
        request,
        "fragments/notification_provider_test.html",
        {"result": result},
    )


@router.post("/jobs/new", response_class=HTMLResponse)
async def create_job(
    request: Request,
) -> Response:
    service = get_services(request).config_editor_service
    submitted = await _schema_form(request)
    result = await service.save_job_form(job_name=None, form=submitted)
    if result.get("saved"):
        job_name = submitted.get("name", "").strip()
        return redirect(f"/config/jobs/{job_name}?saved=1")

    overview = await service.get_overview()
    global_form = await service.get_global_form()
    return template_response(
        request,
        "config_editor_overview.html",
        {
            "overview": overview,
            "global_form": global_form,
            "global_saved": False,
            "global_open": False,
            "new_job_modal_open": True,
            "new_job_form": {
                "name": submitted.get("name", ""),
                "error": result.get("error"),
            },
        },
    )


@router.get("/jobs/{job_name}", response_class=HTMLResponse)
async def edit_job_page(job_name: ResourceName, request: Request) -> HTMLResponse:
    if job_name == "new":
        raise HTTPException(status_code=404, detail="Not found")
    return await render_resource_form(request, JOB_FORM, resource_name=job_name, is_new=False)


@router.post("/jobs/{job_name}", response_class=HTMLResponse)
async def update_job(
    job_name: ResourceName,
    request: Request,
) -> Response:
    return await save_resource_form(
        request, JOB_FORM, await _schema_form(request), resource_name=job_name, is_new=False
    )


@router.get("/jobs/{job_name}/effective", response_class=HTMLResponse)
async def effective_job_page(job_name: ResourceName, request: Request) -> HTMLResponse:
    services = get_services(request)
    view = await services.config_service.get_effective_job_view(job_name)
    overview = await services.config_editor_service.get_overview()
    job_nav = next(
        (item for item in overview.get("jobs", []) if item.get("name") == job_name),
        None,
    )
    return template_response(
        request,
        "config_editor_effective.html",
        {
            "view": view,
            "job_nav": job_nav,
            "job_nav_jobs": overview.get("jobs", []),
            "job_nav_active": "effective",
            "job_nav_resource": None,
        },
    )


@router.get("/jobs/{job_name}/backup-defaults", response_class=HTMLResponse)
async def backup_defaults_page(job_name: ResourceName, request: Request) -> HTMLResponse:
    return await render_resource_form(request, BACKUP_DEFAULTS_FORM, job_name=job_name, is_new=None)


@router.post("/jobs/{job_name}/backup-defaults", response_class=HTMLResponse)
async def update_backup_defaults(job_name: ResourceName, request: Request) -> Response:
    return await save_resource_form(
        request, BACKUP_DEFAULTS_FORM, await _schema_form(request), job_name=job_name, is_new=None
    )


@router.get("/jobs/{job_name}/rclone-defaults", response_class=HTMLResponse)
async def rclone_defaults_page(job_name: ResourceName, request: Request) -> HTMLResponse:
    return await render_resource_form(request, RCLONE_DEFAULTS_FORM, job_name=job_name, is_new=None)


@router.post("/jobs/{job_name}/rclone-defaults", response_class=HTMLResponse)
async def update_rclone_defaults(job_name: ResourceName, request: Request) -> Response:
    return await save_resource_form(
        request, RCLONE_DEFAULTS_FORM, await _schema_form(request), job_name=job_name, is_new=None
    )


@router.get("/jobs/{job_name}/backups/new", response_class=HTMLResponse)
async def new_backup_page(job_name: ResourceName, request: Request) -> HTMLResponse:
    return await render_resource_form(request, BACKUP_FORM, job_name=job_name, is_new=True)


@router.post("/jobs/{job_name}/backups/new", response_class=HTMLResponse)
async def create_backup(
    job_name: ResourceName,
    request: Request,
) -> Response:
    return await save_resource_form(
        request, BACKUP_FORM, await _schema_form(request), job_name=job_name, is_new=True
    )


@router.get("/jobs/{job_name}/backups/{backup_name}", response_class=HTMLResponse)
async def edit_backup_page(
    job_name: ResourceName, backup_name: ResourceName, request: Request
) -> HTMLResponse:
    return await render_resource_form(
        request, BACKUP_FORM, job_name=job_name, resource_name=backup_name, is_new=False
    )


@router.post("/jobs/{job_name}/backups/{backup_name}", response_class=HTMLResponse)
async def update_backup(
    job_name: ResourceName,
    backup_name: ResourceName,
    request: Request,
) -> Response:
    return await save_resource_form(
        request,
        BACKUP_FORM,
        await _schema_form(request),
        job_name=job_name,
        resource_name=backup_name,
        is_new=False,
    )


@router.get("/jobs/{job_name}/workflows/new", response_class=HTMLResponse)
async def new_workflow_page(job_name: ResourceName, request: Request) -> HTMLResponse:
    return await render_resource_form(request, WORKFLOW_FORM, job_name=job_name, is_new=True)


@router.post("/jobs/{job_name}/workflows/new", response_class=HTMLResponse)
async def create_workflow(
    job_name: ResourceName,
    request: Request,
) -> Response:
    return await save_resource_form(
        request, WORKFLOW_FORM, await _schema_form(request), job_name=job_name, is_new=True
    )


@router.get("/jobs/{job_name}/workflows/{workflow_name}", response_class=HTMLResponse)
async def edit_workflow_page(
    job_name: ResourceName, workflow_name: ResourceName, request: Request
) -> HTMLResponse:
    return await render_resource_form(
        request, WORKFLOW_FORM, job_name=job_name, resource_name=workflow_name, is_new=False
    )


@router.post("/jobs/{job_name}/workflows/{workflow_name}", response_class=HTMLResponse)
async def update_workflow(
    job_name: ResourceName,
    workflow_name: ResourceName,
    request: Request,
) -> Response:
    return await save_resource_form(
        request,
        WORKFLOW_FORM,
        await _schema_form(request),
        job_name=job_name,
        resource_name=workflow_name,
        is_new=False,
    )


@router.post("/jobs/{job_name}/delete", response_class=HTMLResponse)
async def delete_job(job_name: ResourceName, request: Request) -> Response:
    return await delete_resource_form(request, JOB_FORM, resource_name=job_name)


@router.post("/jobs/{job_name}/backups/{backup_name}/delete", response_class=HTMLResponse)
async def delete_backup(
    job_name: ResourceName, backup_name: ResourceName, request: Request
) -> Response:
    return await delete_resource_form(
        request,
        BACKUP_FORM,
        job_name=job_name,
        resource_name=backup_name,
        success_redirect=f"/config/jobs/{job_name}",
    )


@router.post("/jobs/{job_name}/workflows/{workflow_name}/delete", response_class=HTMLResponse)
async def delete_workflow(
    job_name: ResourceName, workflow_name: ResourceName, request: Request
) -> Response:
    return await delete_resource_form(
        request,
        WORKFLOW_FORM,
        job_name=job_name,
        resource_name=workflow_name,
        success_redirect=f"/config/jobs/{job_name}",
    )


@router.get("/jobs/{job_name}/rclone/new", response_class=HTMLResponse)
async def new_rclone_page(job_name: ResourceName, request: Request) -> HTMLResponse:
    return await render_resource_form(request, RCLONE_FORM, job_name=job_name, is_new=True)


@router.post("/jobs/{job_name}/rclone/new", response_class=HTMLResponse)
async def create_rclone(
    job_name: ResourceName,
    request: Request,
) -> Response:
    return await save_resource_form(
        request, RCLONE_FORM, await _schema_form(request), job_name=job_name, is_new=True
    )


@router.get("/jobs/{job_name}/rclone/{rclone_name}", response_class=HTMLResponse)
async def edit_rclone_page(
    job_name: ResourceName, rclone_name: ResourceName, request: Request
) -> HTMLResponse:
    return await render_resource_form(
        request, RCLONE_FORM, job_name=job_name, resource_name=rclone_name, is_new=False
    )


@router.post("/jobs/{job_name}/rclone/{rclone_name}", response_class=HTMLResponse)
async def update_rclone(
    job_name: ResourceName,
    rclone_name: ResourceName,
    request: Request,
) -> Response:
    return await save_resource_form(
        request,
        RCLONE_FORM,
        await _schema_form(request),
        job_name=job_name,
        resource_name=rclone_name,
        is_new=False,
    )


@router.post("/jobs/{job_name}/rclone/{rclone_name}/delete", response_class=HTMLResponse)
async def delete_rclone(
    job_name: ResourceName, rclone_name: ResourceName, request: Request
) -> Response:
    return await delete_resource_form(
        request,
        RCLONE_FORM,
        job_name=job_name,
        resource_name=rclone_name,
        success_redirect=f"/config/jobs/{job_name}",
    )
