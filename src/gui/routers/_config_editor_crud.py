from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from src.services.errors import NotFoundServiceError

from ._helpers import get_services, template_response, wants_fragment

FormData = dict[str, Any]
FormGetter = Callable[..., Awaitable[FormData]]
FormSaver = Callable[..., Awaitable[FormData]]
ResourceDeleter = Callable[..., Awaitable[FormData]]


@dataclass(frozen=True)
class EditorFormDescriptor:
    template: str
    success_redirect: str = "/config/jobs"


@dataclass(frozen=True)
class EditorResourceDescriptor:
    template: str
    getter: str
    saver: str
    deleter: str | None
    name_key: str | None
    getter_args: Callable[[str | None, str | None], tuple[object, ...]]
    kwargs: Callable[[str | None, str | None], dict[str, object]]
    success_redirect: str = "/config/jobs"
    edit_url: Callable[[str | None, str | None], str] | None = None


def raise_if_missing(form_data: FormData) -> None:
    """Turn a missing named config resource into the shared 404 error page.

    Die Getter liefern für nicht existierende Ressourcen nur ``not_found`` und
    eine Meldung, aber keine Formularstruktur. Das Formular-Template darf damit
    gar nicht erst gerendert werden.
    """
    if form_data.get("not_found"):
        raise NotFoundServiceError(str(form_data.get("error") or "Not found"))


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=303)


def delete_success_response(request: Request, url: str) -> Response:
    """Redirect after a successful delete.

    HTMX-issued deletes (``hx-post``) get an ``HX-Redirect`` so the client
    does a real navigation to ``url`` instead of htmx swapping the redirect
    target's HTML into the (swap-less) delete form. Non-JS submits keep the
    classic 303 redirect.
    """
    if wants_fragment(request):
        return Response(status_code=200, headers={"HX-Redirect": url})
    return redirect(url)


def saved(request: Request) -> bool:
    return request.query_params.get("saved") == "1"


def saved_url(url: str) -> str:
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{urlencode({'saved': '1'})}"


def context(form_data: FormData, **values: object) -> dict[str, object]:
    return {"form_data": form_data, **values}


def _job_name_for_nav(
    descriptor: EditorResourceDescriptor,
    *,
    job_name: str | None,
    resource_name: str | None,
) -> str | None:
    if descriptor.name_key == "job_name":
        return resource_name
    return job_name


def _job_nav_active(descriptor: EditorResourceDescriptor) -> str | None:
    if descriptor.name_key == "job_name":
        return "settings"
    if descriptor is BACKUP_DEFAULTS_FORM:
        return "backup_defaults"
    if descriptor is RCLONE_DEFAULTS_FORM:
        return "rclone_defaults"
    if descriptor is BACKUP_FORM:
        return "backups"
    if descriptor is WORKFLOW_FORM:
        return "workflows"
    if descriptor is RCLONE_FORM:
        return "rclone"
    return None


async def _job_nav_context(
    request: Request,
    descriptor: EditorResourceDescriptor,
    *,
    job_name: str | None,
    resource_name: str | None,
    is_new: bool | None,
) -> dict[str, object]:
    nav_job_name = _job_name_for_nav(descriptor, job_name=job_name, resource_name=resource_name)
    if nav_job_name is None or is_new is True and descriptor.name_key == "job_name":
        return {}

    overview = await get_services(request).config_editor_service.get_overview()
    job = next(
        (item for item in overview.get("jobs", []) if item.get("name") == nav_job_name),
        None,
    )
    if job is None:
        return {}
    return {
        "job_nav": job,
        "job_nav_jobs": overview.get("jobs", []),
        "job_nav_active": _job_nav_active(descriptor),
        "job_nav_resource": resource_name,
    }


async def render_editor_form(
    request: Request,
    descriptor: EditorFormDescriptor,
    get_form: FormGetter,
    *get_args: object,
    context_values: dict[str, object],
) -> HTMLResponse:
    form_data = await get_form(*get_args)
    raise_if_missing(form_data)
    return template_response(
        request,
        descriptor.template,
        context(form_data, saved=saved(request), **context_values),
    )


def _job_getter_args(_job_name: str | None, resource_name: str | None) -> tuple[object, ...]:
    return () if resource_name is None else (resource_name,)


def _job_kwargs(_job_name: str | None, resource_name: str | None) -> dict[str, object]:
    return {"job_name": resource_name}


def _defaults_getter_args(job_name: str | None, _resource_name: str | None) -> tuple[object, ...]:
    return (job_name,)


def _defaults_kwargs(job_name: str | None, _resource_name: str | None) -> dict[str, object]:
    return {"job_name": job_name}


def _child_getter_args(job_name: str | None, resource_name: str | None) -> tuple[object, ...]:
    return (job_name,) if resource_name is None else (job_name, resource_name)


def _rclone_getter_args(job_name: str | None, resource_name: str | None) -> tuple[object, ...]:
    return (job_name, resource_name)


def _child_kwargs(name_key: str) -> Callable[[str | None, str | None], dict[str, object]]:
    def build(job_name: str | None, resource_name: str | None) -> dict[str, object]:
        return {"job_name": job_name, name_key: resource_name}

    return build


def _resource_context(
    descriptor: EditorResourceDescriptor,
    *,
    job_name: str | None,
    resource_name: str | None,
    is_new: bool | None,
) -> dict[str, object]:
    values: dict[str, object] = {}
    if descriptor.name_key != "job_name":
        values["job_name"] = job_name
    if descriptor.name_key is not None:
        values[descriptor.name_key] = resource_name
    if is_new is not None:
        values["is_new"] = is_new
    return values


def _form_descriptor(
    descriptor: EditorResourceDescriptor, *, success_redirect: str | None = None
) -> EditorFormDescriptor:
    return EditorFormDescriptor(
        descriptor.template,
        success_redirect=success_redirect or descriptor.success_redirect,
    )


async def render_resource_form(
    request: Request,
    descriptor: EditorResourceDescriptor,
    *,
    job_name: str | None = None,
    resource_name: str | None = None,
    is_new: bool | None,
) -> HTMLResponse:
    service = get_services(request).config_editor_service
    get_form = cast(FormGetter, getattr(service, descriptor.getter))
    context_values = _resource_context(
        descriptor, job_name=job_name, resource_name=resource_name, is_new=is_new
    )
    context_values.update(
        await _job_nav_context(
            request,
            descriptor,
            job_name=job_name,
            resource_name=resource_name,
            is_new=is_new,
        )
    )
    return await render_editor_form(
        request,
        _form_descriptor(descriptor),
        get_form,
        *descriptor.getter_args(job_name, resource_name),
        context_values=context_values,
    )


def _submitted_name(submitted: dict[str, str], fallback: str | None) -> str | None:
    value = submitted.get("name")
    if value is None:
        return fallback
    stripped = value.strip()
    return stripped or fallback


def _success_redirect_url(
    descriptor: EditorResourceDescriptor,
    *,
    submitted: dict[str, str],
    job_name: str | None,
    resource_name: str | None,
    is_new: bool | None,
) -> str:
    if is_new is True and descriptor.edit_url is None:
        return descriptor.success_redirect
    if descriptor.edit_url is None:
        return descriptor.success_redirect

    redirect_job_name = job_name
    redirect_resource_name = resource_name
    if descriptor.name_key == "job_name":
        redirect_resource_name = _submitted_name(submitted, resource_name)
    elif descriptor.name_key is not None:
        redirect_resource_name = _submitted_name(submitted, resource_name)

    return saved_url(descriptor.edit_url(redirect_job_name, redirect_resource_name))


async def save_resource_form(
    request: Request,
    descriptor: EditorResourceDescriptor,
    submitted: dict[str, str],
    *,
    job_name: str | None = None,
    resource_name: str | None = None,
    is_new: bool | None,
) -> Response:
    service = get_services(request).config_editor_service
    save_form = cast(FormSaver, getattr(service, descriptor.saver))
    result = await save_form(**descriptor.kwargs(job_name, resource_name), form=submitted)
    if result.get("saved"):
        return redirect(
            _success_redirect_url(
                descriptor,
                submitted=submitted,
                job_name=job_name,
                resource_name=resource_name,
                is_new=is_new,
            )
        )

    get_form = cast(FormGetter, getattr(service, descriptor.getter))
    form_data = await get_form(
        *descriptor.getter_args(job_name, resource_name), submitted=submitted
    )
    form_data["error"] = result.get("error")
    context_values = _resource_context(
        descriptor, job_name=job_name, resource_name=resource_name, is_new=is_new
    )
    context_values.update(
        await _job_nav_context(
            request,
            descriptor,
            job_name=job_name,
            resource_name=resource_name,
            is_new=is_new,
        )
    )
    return template_response(
        request,
        descriptor.template,
        context(form_data, saved=False, **context_values),
    )


async def delete_resource_form(
    request: Request,
    descriptor: EditorResourceDescriptor,
    *,
    job_name: str | None = None,
    resource_name: str | None = None,
    success_redirect: str | None = None,
) -> Response:
    if descriptor.deleter is None:
        raise RuntimeError(f"{descriptor.template} has no delete handler")

    service = get_services(request).config_editor_service
    delete_resource = cast(ResourceDeleter, getattr(service, descriptor.deleter))
    result = await delete_resource(**descriptor.kwargs(job_name, resource_name))
    if result.get("saved"):
        return delete_success_response(request, success_redirect or descriptor.success_redirect)

    get_form = cast(FormGetter, getattr(service, descriptor.getter))
    form_data = await get_form(*descriptor.getter_args(job_name, resource_name))
    raise_if_missing(form_data)
    form_data["error"] = result.get("error")
    context_values = _resource_context(
        descriptor, job_name=job_name, resource_name=resource_name, is_new=False
    )
    context_values.update(
        await _job_nav_context(
            request,
            descriptor,
            job_name=job_name,
            resource_name=resource_name,
            is_new=False,
        )
    )
    return template_response(
        request,
        descriptor.template,
        context(form_data, saved=False, **context_values),
    )


JOB_FORM = EditorResourceDescriptor(
    "config_editor_job.html",
    "get_job_form",
    "save_job_form",
    "delete_job",
    "job_name",
    _job_getter_args,
    _job_kwargs,
    edit_url=lambda _job_name, resource_name: f"/config/jobs/{resource_name}",
)
BACKUP_DEFAULTS_FORM = EditorResourceDescriptor(
    "config_editor_backup_defaults.html",
    "get_backup_defaults_form",
    "save_backup_defaults_form",
    None,
    None,
    _defaults_getter_args,
    _defaults_kwargs,
    edit_url=lambda job_name, _resource_name: f"/config/jobs/{job_name}/backup-defaults",
)
RCLONE_DEFAULTS_FORM = EditorResourceDescriptor(
    "config_editor_rclone_defaults.html",
    "get_rclone_defaults_form",
    "save_rclone_defaults_form",
    None,
    None,
    _defaults_getter_args,
    _defaults_kwargs,
    edit_url=lambda job_name, _resource_name: f"/config/jobs/{job_name}/rclone-defaults",
)
BACKUP_FORM = EditorResourceDescriptor(
    "config_editor_backup.html",
    "get_backup_form",
    "save_backup_form",
    "delete_backup",
    "backup_name",
    _child_getter_args,
    _child_kwargs("backup_name"),
    edit_url=lambda job_name, resource_name: f"/config/jobs/{job_name}/backups/{resource_name}",
)
WORKFLOW_FORM = EditorResourceDescriptor(
    "config_editor_workflow.html",
    "get_workflow_form",
    "save_workflow_form",
    "delete_workflow",
    "workflow_name",
    _child_getter_args,
    _child_kwargs("workflow_name"),
    edit_url=lambda job_name, resource_name: f"/config/jobs/{job_name}/workflows/{resource_name}",
)
RCLONE_FORM = EditorResourceDescriptor(
    "config_editor_rclone.html",
    "get_rclone_form",
    "save_rclone_form",
    "delete_rclone",
    "rclone_name",
    _rclone_getter_args,
    _child_kwargs("rclone_name"),
    edit_url=lambda job_name, resource_name: f"/config/jobs/{job_name}/rclone/{resource_name}",
)
