import json
from collections.abc import AsyncGenerator, AsyncIterable
from datetime import datetime
from typing import cast

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from sse_starlette.sse import EventSourceResponse

from ...services.errors import ServiceError
from ._helpers import (
    ResourceName,
    get_services,
    service_error_to_http,
    template_response,
)

router = APIRouter(prefix="/diagnostics")


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request) -> HTMLResponse:
    data = await get_services(request).log_service.get_logs_view(
        job=request.query_params.get("job"),
        date=request.query_params.get("date"),
    )

    return template_response(request, "diagnostics_logs.html", data)


@router.get("/database", response_class=HTMLResponse)
async def database_page(request: Request) -> HTMLResponse:
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1
    data = await get_services(request).database_inspection_service.get_database_view(
        section=request.query_params.get("section", "system"),
        table=request.query_params.get("table"),
        page=page,
        filter_column=request.query_params.get("filter_column"),
        filter_value=request.query_params.get("filter_value"),
    )
    return template_response(request, "diagnostics_database.html", {"database": data})


@router.get("/logs/{job_name}/raw", response_class=PlainTextResponse)
async def raw_log(job_name: ResourceName, request: Request) -> PlainTextResponse:
    date = request.query_params.get("date") or datetime.now().strftime("%Y-%m-%d")
    try:
        tail = max(0, int(request.query_params.get("tail", "0")))
    except ValueError:
        tail = 0
    try:
        result = await get_services(request).log_service.read_raw(job_name, date, tail=tail)
    except ServiceError as exc:
        return PlainTextResponse(exc.message, status_code=exc.status_code)

    return PlainTextResponse(result["content"])


@router.get("/logs/{job_name}/stream")
async def stream_logs(job_name: ResourceName, request: Request) -> EventSourceResponse:
    try:
        stream = get_services(request).log_service.open_stream(
            job_name, is_disconnected=request.is_disconnected
        )
    except ServiceError as exc:
        raise service_error_to_http(exc) from exc

    async def generate() -> AsyncGenerator[dict[str, str], None]:
        try:
            async for sse_line in cast(AsyncIterable[str], stream):
                raw = str(sse_line).strip()
                if raw.startswith("data: "):
                    try:
                        payload = json.loads(raw[6:])
                        yield {"data": payload.get("line", raw[6:])}
                    except (json.JSONDecodeError, TypeError):
                        yield {"data": raw[6:]}
                else:
                    yield {"data": raw}
        except ServiceError as exc:
            yield {
                "event": "error",
                "data": json.dumps(
                    {"code": exc.code, "message": exc.message},
                    ensure_ascii=True,
                ),
            }
        except Exception as exc:
            yield {
                "event": "error",
                "data": json.dumps(
                    {"code": "stream_error", "message": str(exc)},
                    ensure_ascii=True,
                ),
            }

    return EventSourceResponse(generate())
