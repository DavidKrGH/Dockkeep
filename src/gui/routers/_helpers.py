from collections.abc import Mapping
from typing import Annotated, TypeAlias, cast

from fastapi import HTTPException, Path, Request
from fastapi.responses import HTMLResponse

from ...services.errors import ServiceError
from ...services.factory import ServiceContainer

ResourceName: TypeAlias = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]+$")]
RUN_STATUS_CHANGED_EVENT = "run-status-changed"


def get_services(request: Request) -> ServiceContainer:
    services = getattr(request.app.state, "services", None)
    if services is None:
        raise HTTPException(status_code=503, detail="Service container unavailable")
    return cast(ServiceContainer, services)


def service_error_to_http(exc: ServiceError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message},
    )


def wants_fragment(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def service_error_to_fragment(
    request: Request, exc: ServiceError, template: str = "fragments/restore_error.html"
) -> HTMLResponse:
    """Render a structured service error as an HTMX error fragment.

    The HTTP status stays 200 so HTMX swaps the error fragment into the DOM;
    the structured ``code``/``message`` are rendered inside the fragment.
    """
    return fragment_response(
        request,
        template,
        {"error": {"code": exc.code, "message": exc.message}},
    )


def template_response(
    request: Request,
    template: str,
    context: Mapping[str, object],
    *,
    status_code: int = 200,
) -> HTMLResponse:
    if status_code == 200:
        return cast(
            HTMLResponse,
            request.app.state.templates.TemplateResponse(request, template, dict(context)),
        )
    return cast(
        HTMLResponse,
        request.app.state.templates.TemplateResponse(
            request,
            template,
            dict(context),
            status_code=status_code,
        ),
    )


def fragment_response(
    request: Request,
    template: str,
    context: Mapping[str, object] | None = None,
    *,
    status_code: int = 200,
) -> HTMLResponse:
    response = request.app.state.templates.TemplateResponse(
        request,
        template,
        dict(context or {}),
        status_code=status_code,
    )
    return cast(HTMLResponse, response)


def trigger_run_status_changed(response: HTMLResponse) -> HTMLResponse:
    response.headers["HX-Trigger"] = RUN_STATUS_CHANGED_EVENT
    return response
