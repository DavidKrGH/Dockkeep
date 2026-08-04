"""Configuration file service."""

import asyncio
import os
import tempfile
import tomllib
from pathlib import Path
from typing import Literal, NotRequired, TypedDict

from pydantic import BaseModel, ValidationError

from ..models.config import RawAppConfig, RawJobConfig
from ..models.config_fields import CONFIG_FIELDS, ConfigField, Level, fields_for_level
from ..models.resolved_config import (
    ResolvedAppConfig,
    ResolvedBackupConfig,
    ResolvedCredentials,
    ResolvedJobConfig,
    ResolvedRcloneSyncTaskConfig,
    ResolvedWorkflowConfig,
)
from ..utils.validation import (
    collect_config_warnings,
    load_config,
    load_config_with_raw,
)
from .config_editor_schema import field_label
from .errors import ConfigServiceError, NotFoundServiceError
from .scheduling import next_run_datetime


class ServiceMessageView(TypedDict):
    code: str
    message: str


class RawConfigView(TypedDict):
    path: str
    content: str
    valid: bool
    error: ServiceMessageView | None
    warnings: list[str]
    success: NotRequired[str]


#: Ebene, die den effektiven Wert eines Feldes gesetzt hat. ``"default"``
#: bedeutet: auf keiner Ebene explizit gesetzt.
EffectiveSource = Literal["global", "job", "task", "default"]


class EffectiveFieldView(TypedDict):
    label: str
    value: str
    source: EffectiveSource
    # "items" würde in Jinja mit dict.items() kollidieren, daher "list_items".
    list_items: NotRequired[list[str]]
    note: NotRequired[str]
    kind: NotRequired[str]


class EffectiveGroupView(TypedDict):
    label: str
    fields: list[EffectiveFieldView]


class EffectiveStepView(TypedDict):
    label: str
    kind: str
    target: str


class EffectiveTaskView(TypedDict):
    name: str
    summary_fields: list[EffectiveFieldView]
    main_group: EffectiveGroupView | None
    steps: list[EffectiveStepView]
    groups: list[EffectiveGroupView]
    warnings: list[str]


class EffectiveJobView(TypedDict):
    job_name: str
    job_groups: list[EffectiveGroupView]
    backups: list[EffectiveTaskView]
    workflows: list[EffectiveTaskView]
    rclone_tasks: list[EffectiveTaskView]


_SOURCE_BY_LEVEL: dict[Level, EffectiveSource] = {
    "global": "global",
    "global.backup": "global",
    "global.rclone": "global",
    "global.notifications": "global",
    "job": "job",
    "job.backup": "job",
    "job.rclone": "job",
    "backup": "task",
    "workflow": "task",
    "rclone": "task",
}

_FIELD_BY_LEVEL_AND_KEY: dict[tuple[Level, str], ConfigField] = {
    (f.level, f.key): f for f in CONFIG_FIELDS
}

_PASSWORD_KEYS = frozenset({"password", "password_env", "password_file"})

_CREDENTIAL_LEVELS: tuple[Level, ...] = ("backup", "job.backup", "global.backup")


class _FieldSourceResolver:
    """Bestimmt je Resolved-Feld die Ebene, die den effektiven Wert gesetzt hat.

    Nutzt ``model_fields_set`` der Raw-Modelle: ein Feld gilt als auf einer
    Ebene gesetzt, wenn es dort explizit im TOML stand (inklusive explizitem
    ``false`` und ``[]``). Die Suchreihenfolge folgt der Vererbungskette aus
    ``CONFIG_FIELDS``; ohne Treffer ist der Wert ein Default.
    """

    def __init__(self, level: Level, raw_by_level: dict[Level, BaseModel]) -> None:
        self._raw_by_level = raw_by_level
        self._by_resolved_key = {f.resolved_key: f for f in fields_for_level(level)}

    def source(self, resolved_key: str) -> EffectiveSource:
        field = self._by_resolved_key.get(resolved_key)
        if field is None:
            return "default"
        return self._walk(field.level, field.key)

    def _walk(self, level: Level, key: str) -> EffectiveSource:
        model = self._raw_by_level.get(level)
        if model is not None and key in model.model_fields_set:
            return _SOURCE_BY_LEVEL[level]
        field = _FIELD_BY_LEVEL_AND_KEY.get((level, key))
        if field is None or field.parent_level is None:
            return "default"
        return self._walk(field.parent_level, field.parent_key or field.key)

    def credentials_source(self) -> EffectiveSource:
        """Herkunft des Passwort-Tripels: die Ebene mit dem ersten gesetzten Feld."""
        for level in _CREDENTIAL_LEVELS:
            model = self._raw_by_level.get(level)
            if model is not None and _PASSWORD_KEYS & model.model_fields_set:
                return _SOURCE_BY_LEVEL[level]
        return "default"


#: Resolved-Feldnamen, deren Label im Editor-Schema unter dem Raw-Namen liegt.
_LABEL_KEY_ALIASES: dict[str, str] = {
    "extra_backup_args": "extra_restic_backup_args",
    "extra_forget_args": "extra_restic_forget_args",
    "extra_prune_args": "extra_restic_prune_args",
    "extra_args": "extra_rclone_args",
}

#: Labels für View-Felder ohne Eintrag im Editor-Schema.
_EXTRA_LABELS: dict[str, str] = {
    "password source": "Password source",
    "repository": "Repository",
    "steps": "Steps",
}


def _effective_label(key: str) -> str:
    if key in _EXTRA_LABELS:
        return _EXTRA_LABELS[key]
    return field_label(_LABEL_KEY_ALIASES.get(key, key))


def _effective_value(value: object, source: EffectiveSource) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        if not value:
            # Eine explizit geleerte Liste stoppt die Vererbung – das ist ein
            # gesetzter Zustand, kein fehlender Wert.
            return "empty" if source != "default" else "—"
        return ", ".join(str(item) for item in value)
    return str(value)


def _effective_field(
    key: str,
    value: object,
    source: EffectiveSource,
    *,
    kind: str | None = None,
) -> EffectiveFieldView:
    field: EffectiveFieldView = {
        "label": _effective_label(key),
        "value": _effective_value(value, source),
        "source": source,
    }
    if isinstance(value, list) and value:
        field["list_items"] = [str(item) for item in value]
    if kind is not None:
        field["kind"] = kind
    return field


def _schedule_field(schedule: str | None, source: EffectiveSource) -> EffectiveFieldView:
    field = _effective_field("schedule", schedule, source)
    if schedule is None:
        field["value"] = "manual only"
    else:
        next_time = next_run_datetime(schedule)
        if next_time is not None:
            field["note"] = f"next run {next_time.strftime('%Y-%m-%d %H:%M')}"
    return field


def _group(label: str, fields: list[EffectiveFieldView]) -> EffectiveGroupView:
    return {"label": label, "fields": _visible_fields(fields)}


def _visible_fields(fields: list[EffectiveFieldView]) -> list[EffectiveFieldView]:
    """Behält nur Felder mit wirksamem Wert (kein ``—``-Platzhalter)."""
    return [f for f in fields if f["value"] != "—"]


def _visible_groups(groups: list[EffectiveGroupView]) -> list[EffectiveGroupView]:
    """Behält nur Gruppen, in denen mindestens ein Feld wirksam ist."""
    return [g for g in groups if g["fields"]]


def _effective_group(
    label: str,
    model: BaseModel,
    resolver: _FieldSourceResolver,
    *,
    include: tuple[str, ...] | None = None,
) -> EffectiveGroupView:
    names = include if include is not None else tuple(type(model).model_fields)
    fields = [_effective_field(name, getattr(model, name), resolver.source(name)) for name in names]
    return _group(label, fields)


def _effective_credentials_group(
    credentials: ResolvedCredentials, resolver: _FieldSourceResolver
) -> EffectiveGroupView:
    """Describe the effective password source without rendering the secret itself."""
    field: EffectiveFieldView = {
        "label": _effective_label("password source"),
        "value": "not set",
        "source": resolver.credentials_source(),
    }
    if credentials.password_env is not None:
        field["value"] = credentials.password_env
        field["note"] = "environment variable"
    elif credentials.password_file is not None:
        field["value"] = credentials.password_file
        field["note"] = "password file"
    elif credentials.password is not None:
        field["value"] = "set in config"
    return _group("Credentials", [field])


def _effective_backup_view(
    name: str, backup: ResolvedBackupConfig, resolver: _FieldSourceResolver
) -> EffectiveTaskView:
    warnings: list[str] = []
    if not backup.input.sources and not backup.input.source_files:
        warnings.append(
            "No backup inputs configured. Set 'sources' or 'source_files', "
            "otherwise Restic will only report the error at run time."
        )
    input_group = _effective_group("Input", backup.input, resolver)
    return {
        "name": name,
        "summary_fields": _visible_fields(
            [
                _effective_field(
                    "repository", backup.repository, resolver.source("repository"), kind="path"
                ),
                _schedule_field(backup.schedule, resolver.source("schedule")),
                _effective_field("tags", backup.tags, resolver.source("tags")),
                _effective_field("backend", backup.backend, resolver.source("backend")),
            ]
        ),
        "main_group": input_group if input_group["fields"] else None,
        "steps": [],
        "groups": _visible_groups(
            [
                _effective_credentials_group(backup.credentials, resolver),
                _effective_group("Filters", backup.filters, resolver),
                _effective_group("Retention", backup.retention, resolver),
                _effective_group("Execution", backup.execution, resolver),
                _effective_group("Hooks", backup.hooks, resolver),
                _effective_group("Notifications", backup.notifications, resolver),
                _effective_group(
                    "Timeouts",
                    backup.timeouts,
                    resolver,
                    include=("backup_timeout", "hook_timeout"),
                ),
                _effective_group(
                    "Backend options (restic)", backup.backend_options.restic, resolver
                ),
            ]
        ),
        "warnings": warnings,
    }


def _workflow_steps(steps: list[str]) -> list[EffectiveStepView]:
    """Zerlegt Workflow-Steps (``backup.<name>[.sub]``/``rclone.<name>``) für Verlinkung."""
    views: list[EffectiveStepView] = []
    for step in steps:
        parts = step.split(".")
        views.append({"label": step, "kind": parts[0], "target": parts[1]})
    return views


def _effective_workflow_view(
    name: str, workflow: ResolvedWorkflowConfig, resolver: _FieldSourceResolver
) -> EffectiveTaskView:
    return {
        "name": name,
        "summary_fields": [_schedule_field(workflow.schedule, resolver.source("schedule"))],
        "main_group": None,
        "steps": _workflow_steps(workflow.steps),
        "groups": _visible_groups(
            [
                _effective_group("Hooks", workflow.hooks, resolver),
                _effective_group("Notifications", workflow.notifications, resolver),
                _effective_group(
                    "Timeouts", workflow.timeouts, resolver, include=("hook_timeout",)
                ),
            ]
        ),
        "warnings": [],
    }


def _effective_rclone_view(
    name: str, task: ResolvedRcloneSyncTaskConfig, resolver: _FieldSourceResolver
) -> EffectiveTaskView:
    return {
        "name": name,
        "summary_fields": _visible_fields(
            [
                _effective_field("source", task.source, resolver.source("source"), kind="path"),
                _effective_field("target", task.target, resolver.source("target"), kind="path"),
                _schedule_field(task.schedule, resolver.source("schedule")),
                _effective_field("sync_delete", task.sync_delete, resolver.source("sync_delete")),
            ]
        ),
        "main_group": None,
        "steps": [],
        "groups": _visible_groups(
            [
                _group(
                    "Filters",
                    [
                        _effective_field("exclude", task.exclude, resolver.source("exclude")),
                        _effective_field(
                            "filter_from", task.filter_from, resolver.source("filter_from")
                        ),
                    ],
                ),
                _effective_group("Options", task.options, resolver),
                _effective_group("Hooks", task.hooks, resolver),
                _effective_group("Notifications", task.notifications, resolver),
                _effective_group(
                    "Timeouts", task.timeouts, resolver, include=("rclone_timeout", "hook_timeout")
                ),
            ]
        ),
        "warnings": [],
    }


def _backup_source_resolver(
    raw: RawAppConfig, raw_job: RawJobConfig, name: str
) -> _FieldSourceResolver:
    return _FieldSourceResolver(
        "backup",
        {
            "backup": raw_job.backup.tasks[name],
            "job.backup": raw_job.backup,
            "job": raw_job,
            "global.backup": raw.global_.backup,
            "global": raw.global_,
        },
    )


def _workflow_source_resolver(
    raw: RawAppConfig, raw_job: RawJobConfig, name: str
) -> _FieldSourceResolver:
    return _FieldSourceResolver(
        "workflow",
        {
            "workflow": raw_job.workflow[name],
            "job": raw_job,
            "global": raw.global_,
        },
    )


def _rclone_source_resolver(
    raw: RawAppConfig, raw_job: RawJobConfig, name: str
) -> _FieldSourceResolver:
    return _FieldSourceResolver(
        "rclone",
        {
            "rclone": raw_job.rclone.tasks[name],
            "job.rclone": raw_job.rclone,
            "job": raw_job,
            "global.rclone": raw.global_.rclone,
            "global": raw.global_,
        },
    )


def classify_config_exception(exc: Exception) -> str:
    if isinstance(exc, FileNotFoundError):
        return "file_not_found"
    if isinstance(exc, tomllib.TOMLDecodeError):
        return "toml_error"
    if isinstance(exc, ValidationError):
        return "validation_error"
    if isinstance(exc, ValueError):
        return "value_error"
    if isinstance(exc, OSError):
        return "read_error"
    return "config_error"


class ConfigService:

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path

    def load_active_config(self) -> ResolvedAppConfig:
        try:
            return load_config(self.config_path)
        except Exception as exc:
            recovery_code = classify_config_exception(exc)
            raise ConfigServiceError(
                "config_error",
                str(exc),
                status_code=503,
                recovery_code=recovery_code,
            ) from exc

    def _load_active_config_with_raw(self) -> tuple[RawAppConfig, ResolvedAppConfig]:
        try:
            return load_config_with_raw(self.config_path)
        except Exception as exc:
            recovery_code = classify_config_exception(exc)
            raise ConfigServiceError(
                "config_error",
                str(exc),
                status_code=503,
                recovery_code=recovery_code,
            ) from exc

    async def get_effective_job_view(self, job_name: str) -> EffectiveJobView:
        """Return the fully inherited, effective configuration of one job.

        The view is read-only display data derived from the resolved config
        (``resolve_config()`` output), grouped like ``resolved_config.py``.
        Each field carries the inheritance level that set its value
        (``source``). Secrets are described by their source, never rendered
        as values.
        """
        return await asyncio.to_thread(self._get_effective_job_view_sync, job_name)

    def _get_effective_job_view_sync(self, job_name: str) -> EffectiveJobView:
        raw, config = self._load_active_config_with_raw()
        job = get_job_or_raise(config, job_name)
        raw_job = raw.jobs[job_name]
        job_resolver = _FieldSourceResolver("job", {"job": raw_job, "global": raw.global_})
        return {
            "job_name": job_name,
            "job_groups": _visible_groups(
                [
                    _effective_group("Hooks", job.hooks, job_resolver),
                    _effective_group("Notifications", job.notifications, job_resolver),
                    _effective_group(
                        "Timeouts", job.timeouts, job_resolver, include=("hook_timeout",)
                    ),
                ]
            ),
            "backups": [
                _effective_backup_view(name, backup, _backup_source_resolver(raw, raw_job, name))
                for name, backup in job.backup.items()
            ],
            "workflows": [
                _effective_workflow_view(
                    name, workflow, _workflow_source_resolver(raw, raw_job, name)
                )
                for name, workflow in job.workflows.items()
            ],
            "rclone_tasks": [
                _effective_rclone_view(name, task, _rclone_source_resolver(raw, raw_job, name))
                for name, task in job.rclone.items()
            ],
        }

    async def read_raw(self) -> RawConfigView:
        return await asyncio.to_thread(self._read_raw_sync)

    def _read_raw_sync(self) -> RawConfigView:
        try:
            content = self.config_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {
                "path": str(self.config_path),
                "content": "",
                "valid": False,
                "error": {"code": "file_not_found", "message": "Config file not found"},
                "warnings": [],
            }
        except OSError as exc:
            return {
                "path": str(self.config_path),
                "content": "",
                "valid": False,
                "error": {"code": "read_error", "message": str(exc)},
                "warnings": [],
            }
        validation = self.validate_content(content)
        return {"path": str(self.config_path), "content": content, **validation}

    def validate_content(self, content: str) -> RawConfigView:
        """Validate TOML content through the normal loading pipeline."""
        warnings: list[str] = []
        try:
            tmp: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".toml", delete=False, encoding="utf-8"
                ) as handle:
                    handle.write(content)
                    tmp = Path(handle.name)
                config = load_config(tmp)
                warnings = collect_config_warnings(config)
            finally:
                if tmp and tmp.exists():
                    tmp.unlink()
        except Exception as exc:
            return {
                "path": str(self.config_path),
                "content": content,
                "valid": False,
                "error": {"code": classify_config_exception(exc), "message": str(exc)},
                "warnings": [],
            }
        return {
            "path": str(self.config_path),
            "content": content,
            "valid": True,
            "error": None,
            "warnings": warnings,
        }

    async def save_raw(self, content: str) -> RawConfigView:
        return await asyncio.to_thread(self._save_raw_sync, content)

    def _save_raw_sync(self, content: str) -> RawConfigView:
        validation = self.validate_content(content)
        if not validation["valid"]:
            return validation
        _atomic_write_text(self.config_path, content)
        validation["success"] = "Config saved and validated."
        return validation


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=f".{path.name}.tmp",
            prefix=".dk-",
            dir=path.parent,
            delete=False,
            encoding="utf-8",
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            tmp_path = Path(handle.name)
        if path.exists():
            tmp_path.chmod(path.stat().st_mode & 0o777)
        os.replace(tmp_path, path)
    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink()


def get_job_or_raise(config: ResolvedAppConfig, job_name: str) -> ResolvedJobConfig:
    job = config.jobs.get(job_name)
    if job is None:
        raise NotFoundServiceError(f"Job not found: {job_name}")
    return job
