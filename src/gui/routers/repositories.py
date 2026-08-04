import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...services.errors import ServiceError
from ._helpers import (
    fragment_response,
    get_services,
    service_error_to_http,
    template_response,
)

router = APIRouter(prefix="/repositories")


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def repositories_page(request: Request) -> HTMLResponse:
    view = await get_services(request).repository_browser_service.get_repositories_view()
    return template_response(request, "repositories.html", view)


@router.post("/refresh")
async def repositories_refresh(request: Request) -> RedirectResponse:
    services = get_services(request)
    await services.backup_stats_service.refresh_all_backup_stats()
    return RedirectResponse(url="/repositories", status_code=303)


@router.get("/locations/{location_id}/snapshots", response_class=HTMLResponse)
async def repository_location_snapshots(location_id: str, request: Request) -> HTMLResponse:
    try:
        view = await get_services(request).repository_browser_service.get_location_snapshots_view(
            location_id
        )
    except ServiceError as exc:
        raise service_error_to_http(exc) from exc
    return template_response(request, "repository_location_snapshots.html", view)


@router.get("/locations/{location_id}/snapshots/{snapshot_id}", response_class=HTMLResponse)
async def repository_location_snapshot_detail(
    location_id: str, snapshot_id: str, request: Request
) -> HTMLResponse:
    try:
        view = await get_services(request).repository_browser_service.get_location_snapshot_view(
            location_id, snapshot_id
        )
    except ServiceError as exc:
        raise service_error_to_http(exc) from exc
    return template_response(request, "repository_location_snapshot.html", view)


@router.get(
    "/locations/{location_id}/snapshots/{snapshot_id}/browse",
    response_class=HTMLResponse,
)
async def repository_location_snapshot_browse(
    location_id: str,
    snapshot_id: str,
    request: Request,
    path: str | None = None,
    page: int = 1,
    browser_mode: str = "browse",
) -> HTMLResponse:
    selected_path = path or request.query_params.get("path", "/")
    selected_mode = "restore" if browser_mode == "restore" else "browse"
    try:
        view = await get_services(request).repository_browser_service.browse_location_snapshot_view(
            location_id=location_id,
            snapshot_id=snapshot_id,
            path=selected_path,
            page=page,
        )
    except ServiceError as exc:
        raise service_error_to_http(exc) from exc
    return fragment_response(
        request,
        "fragments/repository_browser.html",
        {**view, "browser_mode": selected_mode},
    )


@router.post(
    "/locations/{location_id}/snapshots/{snapshot_id}/prefetch",
    response_class=HTMLResponse,
)
async def repository_location_snapshot_prefetch(
    location_id: str,
    snapshot_id: str,
    request: Request,
    path: str | None = None,
    page: int = 1,
    browser_mode: str = "browse",
) -> HTMLResponse:
    selected_path = path or request.query_params.get("path", "/")
    selected_mode = "restore" if browser_mode == "restore" else "browse"
    service = get_services(request).repository_browser_service
    try:
        view = await service.prefetch_location_snapshot_view(
            location_id=location_id,
            snapshot_id=snapshot_id,
            path=selected_path,
            page=page,
        )
    except ServiceError as exc:
        raise service_error_to_http(exc) from exc
    return fragment_response(
        request,
        "fragments/repository_browser.html",
        {**view, "browser_mode": selected_mode},
    )


@router.get(
    "/locations/{location_id}/snapshots/{snapshot_id}/restore",
    response_class=HTMLResponse,
)
async def repository_location_snapshot_restore(
    location_id: str, snapshot_id: str, request: Request
) -> HTMLResponse:
    try:
        view = await get_services(request).repository_browser_service.get_location_snapshot_view(
            location_id, snapshot_id
        )
    except ServiceError as exc:
        raise service_error_to_http(exc) from exc
    return template_response(request, "repository_location_snapshot_restore.html", view)


@router.post("/locations/{location_id}/refresh", response_class=HTMLResponse)
async def repository_location_snapshots_refresh(
    location_id: str, request: Request
) -> RedirectResponse:
    services = get_services(request)
    try:
        targets = await services.repository_browser_service.refresh_targets_for_location(
            location_id
        )
        for job_name, backup_name in targets:
            await services.backup_stats_service.refresh_backup_stats(job_name, backup_name)
    except ServiceError as exc:
        raise service_error_to_http(exc) from exc
    return RedirectResponse(url=f"/repositories/locations/{location_id}/snapshots", status_code=303)


@router.post("/locations/{location_id}/delete")
async def repository_location_delete(
    location_id: str,
    request: Request,
) -> RedirectResponse:
    services = get_services(request)
    form = await request.form()
    repository_id = form.get("repository_id")
    try:
        if not isinstance(repository_id, str):
            raise ServiceError("invalid_parameter", "Repository ID is required", 400)
        await asyncio.to_thread(
            services.repository_service.delete_location, repository_id, location_id
        )
    except ServiceError as exc:
        raise service_error_to_http(exc) from exc
    return RedirectResponse(url="/repositories", status_code=303)


@router.post("/locations/{location_id}/merge")
async def repository_location_merge(
    location_id: str,
    request: Request,
) -> RedirectResponse:
    services = get_services(request)
    form = await request.form()
    repository_id = form.get("repository_id")
    target_location_id = form.get("target_location_id")
    try:
        if not isinstance(repository_id, str):
            raise ServiceError("invalid_parameter", "Repository ID is required", 400)
        if not isinstance(target_location_id, str):
            raise ServiceError("invalid_parameter", "Target location ID is required", 400)
        await asyncio.to_thread(
            services.repository_service.merge_location,
            repository_id,
            location_id,
            target_location_id,
        )
    except ServiceError as exc:
        raise service_error_to_http(exc) from exc
    return RedirectResponse(url="/repositories", status_code=303)
