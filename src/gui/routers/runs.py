from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from ...services.errors import ServiceError
from ...services.runs import RunView
from ._helpers import (
    ResourceName,
    fragment_response,
    get_services,
    service_error_to_http,
    template_response,
    trigger_run_status_changed,
)

router = APIRouter(prefix="/runs")
RUNS_PAGE_SIZE = 50


def _with_actions(request: Request) -> bool:
    """Resolve whether run-trigger actions should be rendered for this poll.

    Driven by the ``actions`` query parameter that ``run_status.html`` appends
    to its own poll/cancel URLs (``?actions=0``) whenever it was embedded with
    ``hide_run_action=true`` (Runs list/detail pages, which stay observational
    across every subsequent poll). Dashboard-triggered fragments never set
    ``hide_run_action``, so no query parameter is sent and actions default to
    shown — otherwise the button would vanish for good after the first poll.
    """
    return request.query_params.get("actions") != "0"


def _run_status_response(
    request: Request, run: RunView, *, with_actions: bool, trigger_changed: bool
) -> HTMLResponse:
    response = fragment_response(
        request, "fragments/run_status.html", {"run": run, "hide_run_action": not with_actions}
    )
    if trigger_changed:
        return trigger_run_status_changed(response)
    return response


@router.get("/", response_class=HTMLResponse)
async def runs_page(
    request: Request,
    page: Annotated[int, Query(ge=1)] = 1,
    job: str | None = None,
    task: str | None = None,
    status: str | None = None,
    origin: str | None = None,
) -> HTMLResponse:
    view = await get_services(request).run_service.list_runs_view(
        page=page,
        page_size=RUNS_PAGE_SIZE,
        job=job,
        task=task,
        status=status,
        origin=origin,
    )
    return template_response(request, "runs.html", view)


@router.get("/fragments/list", response_class=HTMLResponse)
async def runs_list_fragment(request: Request) -> HTMLResponse:
    view = await get_services(request).run_service.list_runs_view(
        page=1,
        page_size=RUNS_PAGE_SIZE,
    )
    return fragment_response(request, "fragments/runs_active.html", view)


@router.get("/{run_id}", response_class=HTMLResponse)
async def run_detail(run_id: ResourceName, request: Request) -> HTMLResponse:
    try:
        run = await get_services(request).run_service.get_run_view(run_id)
    except ServiceError as exc:
        raise service_error_to_http(exc) from exc
    except HTTPException:
        raise
    return template_response(request, "run_detail.html", {"run": run})


@router.get("/{run_id}/status", response_class=HTMLResponse)
async def run_status_fragment(run_id: ResourceName, request: Request) -> HTMLResponse:
    """Render the shared run status fragment for HTMX polling.

    Whether run-trigger actions are rendered depends on the ``actions`` query
    parameter (see ``_with_actions``): Runs list/detail pages stay
    observational, dashboard-triggered fragments keep showing actions once the
    run terminates.
    """
    with_actions = _with_actions(request)
    try:
        run = await get_services(request).run_service.get_run_status_view(
            run_id, with_actions=with_actions
        )
    except ServiceError as exc:
        raise service_error_to_http(exc) from exc
    except HTTPException:
        raise
    return _run_status_response(request, run, with_actions=with_actions, trigger_changed=False)


@router.post("/{run_id}/cancel", response_class=HTMLResponse)
async def cancel_run(run_id: ResourceName, request: Request) -> HTMLResponse:
    """Cancel one active run and render its updated status.

    Manual runs are cancelled locally and rendered via the manager-backed
    status viewmodel. Scheduler runs are cancelled over the control socket; the
    returned scheduler dictionary is converted into the shared status fragment
    viewmodel directly because it is not known to the local run manager (and
    never offers run-trigger actions regardless of ``actions``, see
    ``scheduler_status_view``). Whether the manual-run branch renders actions
    depends on the ``actions`` query parameter, same as ``run_status_fragment``.
    """
    with_actions = _with_actions(request)
    service = get_services(request).run_service
    run: RunView
    try:
        result = await service.cancel_run(run_id)
        if isinstance(result, dict):
            run = service.scheduler_status_view(result)
        else:
            run = await service.get_run_status_view(run_id, with_actions=with_actions)
    except ServiceError as exc:
        raise service_error_to_http(exc) from exc
    except HTTPException:
        raise
    return _run_status_response(request, run, with_actions=with_actions, trigger_changed=True)
