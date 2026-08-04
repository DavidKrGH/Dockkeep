from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ._helpers import get_services, template_response

router = APIRouter(prefix="/config")


def _error_message(error: object) -> str | None:
    if error is None:
        return None
    if isinstance(error, dict) and "message" in error:
        return str(error["message"])
    return str(error)


def _render(
    request: Request,
    content: str,
    error: str | None,
    success: str | None,
    warnings: object | None = None,
) -> HTMLResponse:
    return template_response(
        request,
        "config.html",
        {"content": content, "error": error, "success": success, "warnings": warnings or []},
    )


@router.get("", response_class=HTMLResponse)
async def config_page() -> Response:
    return RedirectResponse(url="/config/jobs", status_code=303)


@router.get("/raw", response_class=HTMLResponse)
async def raw_config_page(request: Request) -> HTMLResponse:
    try:
        result = await get_services(request).config_service.read_raw()
    except HTTPException:
        raise
    except Exception as exc:
        return _render(request, "", str(exc), None)

    return _render(
        request,
        result["content"],
        _error_message(result["error"]),
        None,
        result["warnings"],
    )


@router.post("/raw/save", response_class=HTMLResponse)
async def save_config(request: Request, content: str = Form(default="")) -> HTMLResponse:
    try:
        result = await get_services(request).config_service.save_raw(content)
    except HTTPException:
        raise
    except Exception as exc:
        return _render(request, content, str(exc), None)

    return _render(
        request,
        result["content"],
        _error_message(result["error"]),
        (
            None
            if result["error"] is not None
            else result.get("success", "Config saved and validated.")
        ),
        result["warnings"],
    )
