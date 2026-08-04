from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ...services.rclone import RcloneView, RemoteFormView
from ._helpers import (
    fragment_response,
    get_services,
    template_response,
)

router = APIRouter(prefix="/config/remotes")
RCLONE_PAGE_URL = "/config/remotes"


def _error_message(error: object) -> str | None:
    if error is None:
        return None
    if isinstance(error, dict) and "message" in error:
        return str(error["message"])
    return str(error)


def _render_page(request: Request, template: str, data: RcloneView) -> HTMLResponse:
    return template_response(
        request,
        template,
        {
            **data,
            "error": _error_message(data["error"]),
        },
    )


def _redirect_to_rclone_page() -> RedirectResponse:
    return RedirectResponse(url=RCLONE_PAGE_URL, status_code=303)


def _rclone_operation_succeeded(data: RcloneView) -> bool:
    result = data["result"]
    return result is not None and result["ok"]


async def _form_values(request: Request) -> dict[str, str]:
    form = await request.form()
    return {
        key: str(value)
        for key, value in _form_items(form)
        if key not in {"remote_name", "remote_type"}
    }


def _form_query_values(request: Request) -> dict[str, str]:
    values: dict[str, str] = {}
    for key, value in _form_items(request.query_params):
        if key in {"remote_type", "remote"}:
            continue
        if key == "remote_name":
            values["name"] = str(value)
            continue
        values[key] = str(value)
    return values


def _form_items(form: object) -> list[tuple[str, object]]:
    if hasattr(form, "multi_items"):
        return list(form.multi_items())
    if isinstance(form, dict):
        return list(form.items())
    return []


@router.get("", response_class=HTMLResponse)
async def rclone_page(request: Request) -> HTMLResponse:
    service = get_services(request).rclone_service
    data = await service.get_rclone_view()
    return _render_page(request, "rclone.html", data)


@router.get("/raw", response_class=HTMLResponse)
async def rclone_raw_page(request: Request) -> HTMLResponse:
    service = get_services(request).rclone_service
    data = await service.get_rclone_view()
    return _render_page(request, "rclone_raw.html", data)


@router.get("/form", response_class=HTMLResponse)
async def remote_form(
    request: Request,
    remote_type: str = Query("sftp"),
    remote: str | None = Query(None),
) -> HTMLResponse:
    service = get_services(request).rclone_service
    form: RemoteFormView = await service.get_remote_form(
        remote,
        remote_type,
        _form_query_values(request),
    )
    return fragment_response(request, "fragments/rclone_remote_form.html", {"form": form})


@router.post("", response_class=HTMLResponse)
async def create_remote(request: Request) -> Response:
    form = await request.form()
    remote_name = str(form.get("remote_name", ""))
    remote_type = str(form.get("remote_type", ""))
    values = {
        key: str(value)
        for key, value in _form_items(form)
        if key not in {"remote_name", "remote_type"}
    }
    service = get_services(request).rclone_service
    data = await service.create_remote(
        remote_name,
        remote_type,
        values,
    )
    if _rclone_operation_succeeded(data):
        return _redirect_to_rclone_page()
    return _render_page(request, "rclone.html", data)


@router.post("/{remote}", response_class=HTMLResponse)
async def update_remote(remote: str, request: Request) -> Response:
    service = get_services(request).rclone_service
    data = await service.update_remote(
        remote,
        await _form_values(request),
    )
    if _rclone_operation_succeeded(data):
        return _redirect_to_rclone_page()
    return _render_page(request, "rclone.html", data)


@router.post("/{remote}/delete", response_class=HTMLResponse)
async def delete_remote(remote: str, request: Request) -> Response:
    service = get_services(request).rclone_service
    data = await service.delete_remote(remote)
    if _rclone_operation_succeeded(data):
        return _redirect_to_rclone_page()
    return _render_page(request, "rclone.html", data)


@router.post("/raw/save", response_class=HTMLResponse)
async def save_rclone_conf(request: Request, content: str = Form(...)) -> HTMLResponse:
    service = get_services(request).rclone_service
    data = await service.save_config_view(content)
    data["success"] = data["success"] or "rclone.conf saved."
    return _render_page(request, "rclone_raw.html", data)


@router.post("/{remote}/test", response_class=HTMLResponse)
async def test_remote(remote: str, request: Request) -> HTMLResponse:
    service = get_services(request).rclone_service
    result = await service.test_remote_view(remote)
    return fragment_response(
        request,
        "fragments/rclone_remote_status.html",
        {
            "status": result["status"],
            "tone": result["tone"],
            "detail": result["detail"],
            "symbol": result["symbol"],
            "label": result["label"],
        },
    )
