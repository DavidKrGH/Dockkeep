"""Rclone config and diagnostic service."""

import asyncio
import configparser
import os
import re
from pathlib import Path
from typing import TypedDict

from ..core.subprocesses import run_command
from .config import _atomic_write_text
from .errors import ServiceError
from .output import limit_output
from .rclone_config_schema import (
    RcloneBackendSchema,
    RcloneField,
    backend_choices,
    get_backend_schema,
    supported_backend_types,
)

RCLONE_DIAGNOSTIC_TIMEOUT = 15
_REMOTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class ServiceMessageView(TypedDict):
    code: str
    message: str


class RcloneConfigView(TypedDict):
    path: str
    content: str
    exists: bool
    error: ServiceMessageView | None


class RcloneSaveView(TypedDict):
    path: str
    saved: bool


class RemoteRowView(TypedDict):
    name: str
    type: str | None
    supported: bool


class ConfiguredRemoteView(TypedDict):
    name: str
    type: str | None


class RemoteFormView(TypedDict, total=False):
    mode: str
    remote: str | None
    remote_type: str
    remote_name: str
    schema: dict[str, str]
    supported: bool
    backend_choices: list[dict[str, str]]
    fields: list[dict[str, object]]
    errors: dict[str, str]


class RcloneResultView(TypedDict):
    ok: bool
    message: str
    exit_code: int | None
    output: str
    output_truncated: bool
    remotes: list[RemoteRowView]
    form: RemoteFormView | None
    errors: dict[str, str]


class RcloneView(TypedDict):
    remotes: list[str]
    remote_types: dict[str, str | None]
    remote_rows: list[RemoteRowView]
    backend_choices: list[dict[str, str]]
    supported_backend_types: tuple[str, ...]
    form: RemoteFormView
    result: RcloneResultView | None
    content: str
    conf_path: str
    conf_missing: bool
    error: ServiceMessageView | None
    success: str | None


class RemoteDiagnosticView(TypedDict):
    remote: str
    ok: bool
    message: str
    output: str
    output_truncated: bool


class RemoteStatusView(TypedDict):
    status: str
    tone: str
    detail: str | None
    symbol: str
    label: str


class RcloneService:

    def __init__(self, config_dir: Path) -> None:
        self._config_dir = config_dir

    def config_path(self) -> Path:
        env_path = os.environ.get("RCLONE_CONFIG")
        if env_path:
            return Path(env_path)
        return self._config_dir / "rclone.conf"

    def _read_config_sync(self) -> RcloneConfigView:
        path = self.config_path()
        try:
            return {
                "path": str(path),
                "content": path.read_text(encoding="utf-8"),
                "exists": True,
                "error": None,
            }
        except FileNotFoundError:
            return {"path": str(path), "content": "", "exists": False, "error": None}
        except OSError as exc:
            return {
                "path": str(path),
                "content": "",
                "exists": False,
                "error": {"code": "read_error", "message": str(exc)},
            }

    async def save_config(self, content: str) -> RcloneSaveView:
        """Save raw rclone.conf content unchanged."""
        return await asyncio.to_thread(self._save_config_sync, content)

    def _save_config_sync(self, content: str) -> RcloneSaveView:
        path = self.config_path()
        _atomic_write_text(path, content)
        return {"path": str(path), "saved": True}

    async def get_rclone_view(self) -> RcloneView:
        return await asyncio.to_thread(self._get_rclone_view_sync)

    def _get_rclone_view_sync(self) -> RcloneView:
        config = self._read_config_sync()
        remotes_data = _configured_remotes_from_config(self.config_path())
        remote_names = [str(remote["name"]) for remote in remotes_data]
        remote_types = {remote["name"]: remote.get("type") for remote in remotes_data}
        remote_rows = [_remote_row(remote) for remote in remotes_data]
        return {
            "remotes": remote_names,
            "remote_types": remote_types,
            "remote_rows": remote_rows,
            "backend_choices": backend_choices(),
            "supported_backend_types": supported_backend_types(),
            "form": self._empty_form(),
            "result": None,
            "content": config["content"],
            "conf_path": config["path"],
            "conf_missing": not config["exists"],
            "error": config["error"],
            "success": None,
        }

    async def save_config_view(self, content: str) -> RcloneView:
        """Save rclone.conf and return the refreshed page view model."""
        await self.save_config(content)
        data = await self.get_rclone_view()
        data["content"] = content
        data["conf_missing"] = False
        data["success"] = "rclone.conf saved."
        return data

    async def get_remote_form(
        self,
        remote: str | None,
        remote_type: str,
        values: dict[str, str] | None = None,
    ) -> RemoteFormView:
        return await asyncio.to_thread(self._get_remote_form_sync, remote, remote_type, values)

    def _get_remote_form_sync(
        self,
        remote: str | None,
        remote_type: str,
        values: dict[str, str] | None = None,
    ) -> RemoteFormView:
        if remote is not None:
            remote = _validate_remote_name(remote)
            remote_type = self._remote_type(remote) or remote_type
        return self._form(remote=remote, remote_type=remote_type, values=values or {}, errors={})

    async def create_remote(
        self,
        remote_name: str,
        remote_type: str,
        values: dict[str, str],
    ) -> RcloneView:
        remote_name, remote_type, errors = await asyncio.to_thread(
            self._create_remote_validation_sync, remote_name, remote_type, values
        )
        if errors:
            return await self._result_view(
                ok=False,
                message="Remote could not be created.",
                form=self._form(
                    remote=None,
                    remote_type=remote_type,
                    values={"name": remote_name, **values},
                    errors=errors,
                ),
                errors=errors,
            )

        schema = _require_schema(remote_type)
        argv = _build_config_command(
            action="create",
            remote_name=remote_name,
            remote_type=remote_type,
            schema=schema,
            values=values,
            create=True,
        )
        return await self._run_config_command(argv, "Remote created.", None, {})

    def _create_remote_validation_sync(
        self, remote_name: str, remote_type: str, values: dict[str, str]
    ) -> tuple[str, str, dict[str, str]]:
        remote_name = remote_name.strip()
        remote_type = remote_type.strip()
        errors = self._validate_remote_payload(
            remote_name=remote_name,
            remote_type=remote_type,
            values=values,
            create=True,
        )
        if self._remote_exists(remote_name):
            errors["name"] = "Remote already exists."
        return remote_name, remote_type, errors

    async def update_remote(
        self,
        remote_name: str,
        values: dict[str, str],
    ) -> RcloneView:
        remote_name, remote_type, errors = await asyncio.to_thread(
            self._update_remote_validation_sync, remote_name, values
        )
        if remote_type is None:
            return await self._result_view(
                ok=False,
                message="Remote not found.",
                errors={"name": "Remote not found."},
            )
        if errors:
            return await self._result_view(
                ok=False,
                message="Remote could not be saved.",
                form=self._form(
                    remote=remote_name,
                    remote_type=remote_type,
                    values=values,
                    errors=errors,
                ),
                errors=errors,
            )

        schema = _require_schema(remote_type)
        argv = _build_config_command(
            action="update",
            remote_name=remote_name,
            remote_type=remote_type,
            schema=schema,
            values=values,
            create=False,
        )
        if len(argv) == 5:
            return await self._result_view(
                ok=True,
                message="No changes submitted.",
                form=self._form(remote=remote_name, remote_type=remote_type, values={}, errors={}),
                errors={},
            )
        return await self._run_config_command(argv, "Remote saved.", None, {})

    def _update_remote_validation_sync(
        self, remote_name: str, values: dict[str, str]
    ) -> tuple[str, str | None, dict[str, str]]:
        remote_name = _validate_remote_name(remote_name)
        remote_type = self._remote_type(remote_name)
        if remote_type is None:
            return remote_name, None, {}
        errors = self._validate_remote_payload(
            remote_name=remote_name,
            remote_type=remote_type,
            values=values,
            create=False,
        )
        return remote_name, remote_type, errors

    async def delete_remote(self, remote_name: str) -> RcloneView:
        """Delete a valid rclone remote and return a page update view model."""
        remote_name = _validate_remote_name(remote_name)
        argv = ["rclone", "config", "delete", remote_name]
        return await self._run_config_command(argv, "Remote deleted.", self._empty_form(), {})

    async def test_remote(self, remote: str) -> RemoteDiagnosticView:
        remote = _validate_remote_name(remote)
        try:
            result = await run_command(
                ["rclone", "lsd", f"{remote}:"],
                timeout=RCLONE_DIAGNOSTIC_TIMEOUT,
            )
            raw_output = (result.stdout + result.stderr).strip()
            output, truncated = limit_output(raw_output)
            ok = result.returncode == 0
            return {
                "remote": remote,
                "ok": ok,
                "message": "reachable" if ok else "not reachable",
                "output": output,
                "output_truncated": truncated,
            }
        except asyncio.TimeoutError as exc:
            output, truncated = limit_output(str(exc))
            return {
                "remote": remote,
                "ok": False,
                "message": "timeout",
                "output": output,
                "output_truncated": truncated,
            }
        except OSError as exc:
            output, truncated = limit_output(str(exc))
            return {
                "remote": remote,
                "ok": False,
                "message": "rclone unavailable",
                "output": output,
                "output_truncated": truncated,
            }

    async def test_remote_view(self, remote: str) -> RemoteStatusView:
        result = await self.test_remote(remote)
        return _remote_status_view(result)

    def _empty_form(self) -> RemoteFormView:
        default_type = supported_backend_types()[0]
        return self._form(remote=None, remote_type=default_type, values={}, errors={})

    def _form(
        self,
        *,
        remote: str | None,
        remote_type: str,
        values: dict[str, str],
        errors: dict[str, str],
    ) -> RemoteFormView:
        schema = get_backend_schema(remote_type)
        mode = "edit" if remote else "create"
        if schema is None:
            return {
                "mode": mode,
                "remote": remote,
                "remote_type": remote_type,
                "supported": False,
                "backend_choices": backend_choices(),
                "fields": [],
                "errors": errors,
            }
        stored_values = self._remote_values(remote) if remote else {}
        fields: list[dict[str, object]] = []
        for field in schema.fields:
            submitted = field.key in values
            value = values.get(field.key, stored_values.get(field.key, ""))
            fields.append(_field_view(field, value, mode, reveal_value=submitted))
        return {
            "mode": mode,
            "remote": remote,
            "remote_type": remote_type,
            "remote_name": values.get("name", remote or ""),
            "schema": {
                "key": schema.key,
                "label": schema.label,
                "description": schema.description,
            },
            "supported": True,
            "backend_choices": backend_choices(),
            "fields": fields,
            "errors": errors,
        }

    def _validate_remote_payload(
        self,
        *,
        remote_name: str,
        remote_type: str,
        values: dict[str, str],
        create: bool,
    ) -> dict[str, str]:
        errors: dict[str, str] = {}
        if not _valid_remote_name(remote_name):
            errors["name"] = "Remote name is invalid."
        schema = get_backend_schema(remote_type)
        if schema is None:
            errors["type"] = "Backend is not supported by the schema editor."
            return errors
        for schema_field in schema.fields:
            value = values.get(schema_field.key, "").strip()
            required = schema_field.required and (create or not schema_field.rclone_password)
            if required and not value:
                errors[schema_field.key] = "Required field."
            if value:
                field_error = _validate_field_value(schema_field, value)
                if field_error:
                    errors[schema_field.key] = field_error
        if remote_type == "s3":
            _validate_s3(values, errors)
        return errors

    def _remote_exists(self, remote_name: str) -> bool:
        return remote_name in _parse_remote_types(self.config_path())

    def _remote_type(self, remote_name: str) -> str | None:
        return _parse_remote_types(self.config_path()).get(remote_name)

    def _remote_values(self, remote_name: str | None) -> dict[str, str]:
        if remote_name is None:
            return {}
        return _parse_remote_values(self.config_path()).get(remote_name, {})

    async def _run_config_command(
        self,
        argv: list[str],
        success_message: str,
        form: RemoteFormView | None,
        errors: dict[str, str],
    ) -> RcloneView:
        try:
            result = await run_command(argv, timeout=RCLONE_DIAGNOSTIC_TIMEOUT)
            raw_output = (result.stdout + result.stderr).strip()
            output, truncated = limit_output(raw_output)
            ok = result.returncode == 0
            return await self._result_view(
                ok=ok,
                message=success_message if ok else "rclone reported an error.",
                exit_code=result.returncode,
                output=output,
                output_truncated=truncated,
                form=form if not ok else self._empty_form(),
                errors=errors,
            )
        except asyncio.TimeoutError as exc:
            output, truncated = limit_output(str(exc))
            return await self._result_view(
                ok=False,
                message="rclone config timed out.",
                output=output,
                output_truncated=truncated,
                form=form,
                errors=errors,
            )
        except OSError as exc:
            output, truncated = limit_output(str(exc))
            return await self._result_view(
                ok=False,
                message="rclone is not available.",
                output=output,
                output_truncated=truncated,
                form=form,
                errors=errors,
            )

    async def _result_view(
        self,
        *,
        ok: bool,
        message: str,
        exit_code: int | None = None,
        output: str = "",
        output_truncated: bool = False,
        form: RemoteFormView | None = None,
        errors: dict[str, str] | None = None,
    ) -> RcloneView:
        data = await self.get_rclone_view()
        result: RcloneResultView = {
            "ok": ok,
            "message": message,
            "exit_code": exit_code,
            "output": output,
            "output_truncated": output_truncated,
            "remotes": data["remote_rows"],
            "form": form,
            "errors": errors or {},
        }
        data["result"] = result
        data["form"] = form or data["form"]
        if ok:
            data["success"] = message
        else:
            data["error"] = {"code": "rclone_config_error", "message": message}
        return data


def _configured_remotes_from_config(path: Path) -> list[ConfiguredRemoteView]:
    remote_types = {
        name: remote_type
        for name, remote_type in _parse_remote_types(path).items()
        if _valid_remote_name(name)
    }
    return [{"name": name, "type": remote_types.get(name)} for name in sorted(remote_types)]


def _remote_row(remote: ConfiguredRemoteView) -> RemoteRowView:
    remote_type = remote.get("type")
    return {
        "name": str(remote["name"]),
        "type": remote_type,
        "supported": remote_type in supported_backend_types() if remote_type is not None else False,
    }


def _remote_status_view(result: RemoteDiagnosticView) -> RemoteStatusView:
    if bool(result.get("ok", False)):
        return {
            "status": "ok",
            "tone": "success",
            "detail": None,
            "symbol": "OK",
            "label": "Reachable",
        }
    if result.get("message") == "timeout":
        return {
            "status": "warning",
            "tone": "warning",
            "detail": None,
            "symbol": "!",
            "label": "Timeout",
        }
    return {
        "status": "error",
        "tone": "danger",
        "detail": str(result.get("message", "")),
        "symbol": "X",
        "label": "Not reachable",
    }


def _parse_remote_types(path: Path) -> dict[str, str | None]:
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error):
        return {}
    return {
        section: parser.get(section, "type") if parser.has_option(section, "type") else None
        for section in parser.sections()
    }


def _parse_remote_values(path: Path) -> dict[str, dict[str, str]]:
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error):
        return {}
    values: dict[str, dict[str, str]] = {}
    for section in parser.sections():
        if _valid_remote_name(section):
            values[section] = {key: value for key, value in parser.items(section)}
    return values


def _validate_remote_name(remote: str) -> str:
    if not _valid_remote_name(remote):
        raise ServiceError("invalid_parameter", "Invalid rclone remote name", 400)
    return remote


def _valid_remote_name(remote: str) -> bool:
    return bool(_REMOTE_RE.fullmatch(remote))


def _require_schema(remote_type: str) -> RcloneBackendSchema:
    schema = get_backend_schema(remote_type)
    if schema is None:
        raise ServiceError("invalid_parameter", "Unsupported rclone backend type", 400)
    return schema


def _field_view(
    field: RcloneField,
    value: str,
    mode: str,
    *,
    reveal_value: bool = False,
) -> dict[str, object]:
    return {
        "name": field.key,
        "label": field.label,
        "kind": field.kind,
        "required": field.required and (mode == "create" or not field.rclone_password),
        "choices": field.choices,
        "placeholder": field.placeholder,
        "help_text": field.help_text,
        "value": "" if field.rclone_password and not reveal_value else value,
        "rclone_password": field.rclone_password,
        "secret": field.secret,
    }


def _validate_field_value(field: RcloneField, value: str) -> str | None:
    if field.kind == "number":
        try:
            number = int(value)
        except ValueError:
            return "Must be a positive integer."
        if number <= 0:
            return "Must be a positive integer."
    if field.kind == "bool" and value not in {"true", "false"}:
        return "Must be true or false."
    if field.kind == "select" and value not in field.choices:
        return "Invalid selection."
    return None


def _validate_s3(values: dict[str, str], errors: dict[str, str]) -> None:
    env_auth = values.get("env_auth", "").strip().lower() == "true"
    if env_auth:
        return
    provider = values.get("provider", "").strip()
    if not values.get("access_key_id", "").strip():
        errors.setdefault("access_key_id", "Required for S3 without env_auth.")
    if not values.get("secret_access_key", "").strip():
        errors.setdefault("secret_access_key", "Required for S3 without env_auth.")
    if provider in {"Minio", "Cloudflare", "Other"} and not values.get("endpoint", "").strip():
        errors.setdefault("endpoint", "Required for this S3 provider.")


def _build_config_command(
    *,
    action: str,
    remote_name: str,
    remote_type: str,
    schema: RcloneBackendSchema,
    values: dict[str, str],
    create: bool,
) -> list[str]:
    argv = ["rclone", "config", action, remote_name]
    if action == "create":
        argv.append(remote_type)
    obscure = False
    for field in schema.fields:
        value = values.get(field.key, "").strip()
        if not value:
            continue
        argv.extend([field.key, _normalize_config_value(field, value)])
        if field.rclone_password:
            obscure = True
    argv.append("--non-interactive")
    if obscure:
        argv.append("--obscure")
    return argv


def _normalize_config_value(field: RcloneField, value: str) -> str:
    if field.kind == "bool":
        return value.lower()
    return value
