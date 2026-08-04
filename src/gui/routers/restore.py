import asyncio
from collections.abc import Coroutine
from typing import TypeVar

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ...models.resolved_config import ResolvedBackupConfig
from ...services.errors import ServiceError
from ._helpers import (
    ResourceName,
    fragment_response,
    get_services,
    service_error_to_fragment,
    trigger_run_status_changed,
)

router = APIRouter(prefix="/restore")

_T = TypeVar("_T")


def _restore_status_response(
    request: Request,
    template: str,
    context: dict[str, object],
    *,
    trigger_changed: bool,
) -> HTMLResponse:
    response = fragment_response(request, template, context)
    if trigger_changed:
        return trigger_run_status_changed(response)
    return response


# Synthetic identity used for ``RestoreRequest.job``/``.backup`` when a restore
# runs on ad-hoc, never-persisted credentials (no active config, no donor
# config exists anywhere for the location's repository identity). These are
# pure display/identity labels surfaced in run history and logs. This value is
# reserved in config validation so it cannot collide with a real user job.
_ADHOC_JOB_NAME = "__dockkeep_adhoc_restore__"


async def _resolve_restore_identity(
    request: Request,
    job_name: str,
    backup_name: str,
    location_id: str,
    *,
    adhoc_password: str | None,
    adhoc_password_env: str | None,
    adhoc_password_file: str | None,
) -> tuple[str, str, ResolvedBackupConfig | None]:
    """Resolve the (job, backup, resolved_backup) identity for a restore request.

    When ``location_id`` is given, it is the sole source of truth: any
    client-supplied ``job_name``/``backup_name`` form fields are ignored and
    identity is re-resolved fresh via the repository browser service. When
    ``location_id`` is empty, behavior is unchanged: the plain form fields
    are used and no override config is applied.

    If no active config and no donor config exist for the location, the
    caller-supplied ad-hoc credential fields (at most one of which may be
    set) are used to build a transient, never-persisted config instead of
    raising a hard error. If none of them are set either, the existing
    controlled ``no_credentials`` error is raised.
    """
    if not location_id:
        return job_name, backup_name, None
    repository_browser_service = get_services(request).repository_browser_service
    resolved = await asyncio.to_thread(
        repository_browser_service.resolve_backup_config_for_location, location_id
    )
    if resolved is not None:
        return resolved
    if adhoc_password is None and adhoc_password_env is None and adhoc_password_file is None:
        raise ServiceError(
            "no_credentials",
            "No configured backup currently provides credentials for this location; "
            "provide a password to proceed",
            409,
        )
    adhoc_backup = await asyncio.to_thread(
        repository_browser_service.build_adhoc_backup_config,
        location_id,
        password=adhoc_password,
        password_env=adhoc_password_env,
        password_file=adhoc_password_file,
    )
    return _ADHOC_JOB_NAME, location_id, adhoc_backup


@router.post("/preview", response_class=HTMLResponse)
async def preview_restore(
    request: Request,
    job_name: str = Form(""),
    backup_name: str = Form(""),
    location_id: str = Form(""),
    snapshot_id: str = Form(...),
    restore_mode: str = Form("pattern"),
    snapshot_paths: str = Form(""),
    include_patterns: str = Form(""),
    exclude_patterns: str = Form(""),
    target: str = Form(""),
    overwrite: bool = Form(False),
    adhoc_password: str = Form(""),
    adhoc_password_env: str = Form(""),
    adhoc_password_file: str = Form(""),
) -> HTMLResponse:
    try:
        job_name, backup_name, resolved_backup = await _resolve_restore_identity(
            request,
            job_name,
            backup_name,
            location_id,
            adhoc_password=_optional_text(adhoc_password),
            adhoc_password_env=_optional_text(adhoc_password_env),
            adhoc_password_file=_optional_text(adhoc_password_file),
        )
        preview = await _await_request_bound_preview(
            request,
            get_services(request).restore_service.preview_restore(
                job_name,
                backup_name,
                snapshot_id,
                mode=restore_mode,
                snapshot_paths=_parse_lines(snapshot_paths),
                include_patterns=_parse_lines(include_patterns),
                exclude_patterns=_parse_lines(exclude_patterns),
                target=_optional_text(target),
                overwrite=overwrite,
                resolved_backup=resolved_backup,
            ),
        )
    except ServiceError as exc:
        return service_error_to_fragment(request, exc)

    return fragment_response(
        request,
        "fragments/restore_preview.html",
        {"preview": get_services(request).restore_service.preview_view(preview)},
    )


@router.post("/start", response_class=HTMLResponse)
async def start_restore(
    request: Request,
    job_name: str = Form(""),
    backup_name: str = Form(""),
    location_id: str = Form(""),
    snapshot_id: str = Form(...),
    restore_mode: str = Form("pattern"),
    snapshot_paths: str = Form(""),
    include_patterns: str = Form(""),
    exclude_patterns: str = Form(""),
    target: str = Form(""),
    overwrite: bool = Form(False),
    adhoc_password: str = Form(""),
    adhoc_password_env: str = Form(""),
    adhoc_password_file: str = Form(""),
) -> HTMLResponse:
    try:
        job_name, backup_name, resolved_backup = await _resolve_restore_identity(
            request,
            job_name,
            backup_name,
            location_id,
            adhoc_password=_optional_text(adhoc_password),
            adhoc_password_env=_optional_text(adhoc_password_env),
            adhoc_password_file=_optional_text(adhoc_password_file),
        )
        record = await get_services(request).restore_service.start_restore(
            job_name,
            backup_name,
            snapshot_id,
            mode=restore_mode,
            snapshot_paths=_parse_lines(snapshot_paths),
            include_patterns=_parse_lines(include_patterns),
            exclude_patterns=_parse_lines(exclude_patterns),
            target=_optional_text(target),
            overwrite=overwrite,
            resolved_backup=resolved_backup,
        )
    except ServiceError as exc:
        return service_error_to_fragment(request, exc)

    return _restore_status_response(
        request,
        "fragments/restore_start.html",
        {"restore": get_services(request).restore_service.restore_view(record)},
        trigger_changed=True,
    )


@router.get("/{run_id}/status", response_class=HTMLResponse)
async def restore_status_fragment(run_id: ResourceName, request: Request) -> HTMLResponse:
    try:
        view = await get_services(request).restore_service.get_restore_view(run_id)
    except ServiceError as exc:
        return service_error_to_fragment(request, exc)
    return _restore_status_response(
        request,
        "fragments/restore_status.html",
        {"restore": view},
        trigger_changed=False,
    )


@router.post("/{run_id}/cancel", response_class=HTMLResponse)
async def cancel_restore(run_id: ResourceName, request: Request) -> HTMLResponse:
    try:
        await get_services(request).restore_service.cancel_restore(run_id)
        view = await get_services(request).restore_service.get_restore_view(run_id)
    except ServiceError as exc:
        return service_error_to_fragment(request, exc)
    return _restore_status_response(
        request,
        "fragments/restore_status.html",
        {"restore": view},
        trigger_changed=True,
    )


async def _await_request_bound_preview(
    request: Request, preview: Coroutine[object, object, _T]
) -> _T:
    preview_task: asyncio.Task[_T] = asyncio.create_task(preview)
    disconnect_task = asyncio.create_task(_wait_for_disconnect(request))
    try:
        done, _pending = await asyncio.wait(
            {preview_task, disconnect_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if preview_task in done:
            return await preview_task
        preview_task.cancel()
        await asyncio.gather(preview_task, return_exceptions=True)
        raise asyncio.CancelledError
    finally:
        disconnect_task.cancel()
        if not preview_task.done():
            preview_task.cancel()
        await asyncio.gather(preview_task, disconnect_task, return_exceptions=True)


async def _wait_for_disconnect(request: Request) -> None:
    while not await request.is_disconnected():
        await asyncio.sleep(0.1)


def _parse_lines(raw_value: str) -> list[str] | None:
    values = [line.strip() for line in raw_value.splitlines() if line.strip()]
    return values or None


def _optional_text(value: str) -> str | None:
    stripped = value.strip()
    return stripped or None
