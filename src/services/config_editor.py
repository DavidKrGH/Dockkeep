"""Structured config editor service for config.toml TOML manipulation."""

import asyncio
import logging
import threading
from collections.abc import MutableMapping
from datetime import datetime, timedelta
from functools import wraps
from typing import Any, cast

import tomlkit
from tomlkit.toml_document import TOMLDocument

from ..models.config import _NAME_RE, RawGlobalConfig
from ..models.resolved_config import (
    ResolvedMailNotificationConfig,
    ResolvedPushoverNotificationConfig,
)
from ..notifications.dispatcher import NotificationDispatcher
from ..notifications.events import (
    NotificationEvent,
    NotificationReportEvent,
    NotificationReportRunSummary,
)
from ..notifications.providers.mail import MailProvider
from ..notifications.providers.pushover import PushoverProvider
from ..utils.timeouts import env_timeout
from ..utils.validation import _validate_sources as _validate_raw_sources
from .config import ConfigService
from .config_editor_schema import (
    BACKUP_FIELDS,
    GLOBAL_BACKUP_FIELDS,
    GLOBAL_FIELDS,
    GLOBAL_NOTIFICATION_FIELDS,
    GLOBAL_RCLONE_FIELDS,
    JOB_BACKUP_DEFAULTS_FIELDS,
    JOB_FIELDS,
    JOB_RCLONE_DEFAULTS_FIELDS,
    MAIL_FIELDS,
    PUSHOVER_FIELDS,
    RCLONE_FIELDS,
    WORKFLOW_FIELDS,
    EditorField,
    apply_fields,
    effective_values,
    field_views,
    pseudo_table_from_form,
)
from .errors import ConfigServiceError

logger = logging.getLogger(__name__)

_RESERVED_JOB_NAMES = {"_system", "__dockkeep_adhoc_restore__"}

# Editor-Routen reservieren dieses Pfadsegment unter jeder Ressourcen-Kollektion
# (/config/jobs/new, /config/jobs/<job>/backups/new, ...). Eine so benannte
# Ressource waere im strukturierten Editor unerreichbar.
_RESERVED_EDITOR_SEGMENTS = {"new"}


def _serialized_write(method: Any) -> Any:
    """Serialize one structured read-modify-write operation per service."""

    @wraps(method)
    def wrapper(self: "ConfigEditorService", *args: object, **kwargs: object) -> dict[str, Any]:
        with self._write_lock:
            return cast(dict[str, Any], method(self, *args, **kwargs))

    return wrapper


def _validate_name(name: str, kind: str, reserved: set[str] | None = None) -> str | None:
    """Return an error string if the name is invalid, else None.

    Args:
        name: The candidate task/job name.
        kind: Human-readable kind used in error messages.
        reserved: Optional set of names that collide with scalar default keys of
            a mixed container table; such names cannot be used as task names.
    """
    if not name:
        return f"{kind} name must not be empty"
    if not _NAME_RE.match(name):
        return (
            f"Invalid {kind} name {name!r}: "
            "only letters, digits, hyphens, and underscores are allowed"
        )
    if reserved is not None and name in reserved:
        return (
            f"Invalid {kind} name {name!r}: this name is reserved for a configuration "
            "field and cannot be used as a task name"
        )
    if kind == "job" and name in _RESERVED_JOB_NAMES:
        return f"Invalid {kind} name {name!r}: this name is reserved"
    if name in _RESERVED_EDITOR_SEGMENTS:
        return (
            f"Invalid {kind} name {name!r}: this name is reserved for the " "editor's create route"
        )
    return None


def _validate_sources(sources: list[str]) -> str | None:
    """Return an error string if any source violates the raw source rules.

    Delegates to the raw config validator so the editor pre-check stays in sync
    with the canonical rule (absolute path, no empty entries, no control
    characters) instead of being a weaker partial copy.
    """
    try:
        _validate_raw_sources("source", sources)
    except ValueError as exc:
        return str(exc)
    return None


def _validate_workflow_steps(
    steps: list[str], backup_names: set[str], rclone_names: set[str]
) -> str | None:
    for step in steps:
        parts = step.split(".")
        if parts[0] == "rclone":
            if len(parts) != 2:
                return f"Invalid Rclone step format {step!r}. Expected: 'rclone.<name>'."
            if parts[1] not in rclone_names:
                return f"Step {step!r} references unknown Rclone task {parts[1]!r}"
            continue
        if (
            parts[0] != "backup"
            or len(parts) not in {2, 3}
            or (len(parts) == 3 and parts[2] not in {"backup", "retention", "cleanup"})
        ):
            return (
                f"Invalid step format {step!r}. "
                "Expected: 'backup.<name>' or 'backup.<name>.backup|retention|cleanup'."
            )
        if parts[1] not in backup_names:
            return f"Step {step!r} references unknown backup {parts[1]!r}"
    return None


def _workflow_step_view(step: str) -> dict[str, str]:
    parts = step.split(".")
    if parts[0] == "rclone" and len(parts) == 2:
        return {"kind": "rclone", "task": parts[1], "action": ""}
    if parts[0] == "backup" and len(parts) in {2, 3}:
        return {
            "kind": "backup",
            "task": parts[1],
            "action": parts[2] if len(parts) == 3 else "all",
        }
    return {"kind": "backup", "task": "", "action": "all"}


TomlMapping = MutableMapping[str, Any]

_RCLONE_PRIMARY_FIELDS = tuple(item for item in RCLONE_FIELDS if item.key in {"source", "target"})
_RCLONE_OPTION_FIELDS = tuple(
    item for item in RCLONE_FIELDS if item.key not in {"source", "target"}
)


def _test_notification_event(provider: str) -> NotificationEvent:
    now = datetime.now()
    return NotificationEvent(
        job_name="dockkeep",
        task_type="workflow",
        task_name=f"{provider}_provider_test",
        status="success",
        started_at=now,
        finished_at=now,
        dry_run=False,
        message=(
            "This is a Dockkeep test notification. If you received this message, "
            "the notification provider is configured correctly."
        ),
    )


def _test_report_event() -> NotificationReportEvent:
    now = datetime.now()
    return NotificationReportEvent(
        window_start=now - timedelta(hours=6),
        window_end=now,
        generated_at=now,
        status_counts={"success": 1, "failed": 1},
        runs=(
            NotificationReportRunSummary(
                origin="scheduler",
                job="dockkeep",
                target="dockkeep.backup.report_test_ok",
                status="success",
                started_at=now - timedelta(minutes=5),
                finished_at=now,
                duration_seconds=300,
                dry_run=False,
            ),
            NotificationReportRunSummary(
                origin="scheduler",
                job="dockkeep",
                target="dockkeep.backup.report_test_failed",
                status="failed",
                started_at=now - timedelta(minutes=3),
                finished_at=now,
                duration_seconds=15,
                dry_run=False,
                error="This is a test failure to preview the report layout.",
            ),
        ),
    )


def _as_mapping(value: object, context: str) -> TomlMapping:
    if not isinstance(value, MutableMapping):
        raise TypeError(f"{context} must be a TOML table")
    return cast(TomlMapping, value)


def _mapping_or_empty(data: TomlMapping, key: str, context: str) -> TomlMapping:
    value = data.get(key)
    if value is None:
        return {}
    return _as_mapping(value, context)


def _get_or_create_table(data: TomlMapping, key: str, context: str) -> TomlMapping:
    value = data.get(key)
    if value is None:
        data[key] = tomlkit.table()
        value = data[key]
    return _as_mapping(value, context)


def _scalar_defaults_only(table: TomlMapping, fields: tuple[EditorField, ...]) -> TomlMapping:
    """Return only the scalar default keys of a mixed container table.

    The container tables ``[jobs.<job>.backup]`` and ``[jobs.<job>.rclone]``
    hold both scalar default fields and nested task subtables. Sibling
    subtables (``[jobs.<job>.backup.<name>]``) must be ignored when reading or
    rendering the defaults, so this returns only the recognised scalar/list
    keys (and credentials) and never the task subtables.
    """
    allowed = {item.key for item in fields}
    allowed.update({"password", "password_env", "password_file"})
    return {key: value for key, value in table.items() if key in allowed}


class ConfigEditorService:
    """Use-case service for structured editing of the config.toml configuration.

    All public methods are safe to call even when the config is broken; they
    return dicts with an ``error`` key instead of raising.
    """

    def __init__(self, config_service: ConfigService) -> None:
        self._cfg = config_service
        self._write_lock = threading.RLock()

    async def get_global_form(self, submitted: dict[str, str] | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_global_form_sync, submitted)

    def _get_global_form_sync(self, submitted: dict[str, str] | None = None) -> dict[str, Any]:
        """Return editable global settings.

        Args:
            submitted: When provided, field values are taken from this dict (submitted
                form values) instead of from the saved TOML, so user input is preserved
                after a failed save attempt.
        """
        password_configured = False
        try:
            parsed = self._parse_raw()
            global_data = _mapping_or_empty(parsed, "global", "global")
            backup_defaults = _mapping_or_empty(global_data, "backup", "global.backup")
            if submitted is not None:
                password_configured = "password" in backup_defaults
            rclone = _mapping_or_empty(global_data, "rclone", "global.rclone")
            notifications = _mapping_or_empty(global_data, "notifications", "global.notifications")
            mail = _mapping_or_empty(notifications, "mail", "global.notifications.mail")
            pushover = _mapping_or_empty(notifications, "pushover", "global.notifications.pushover")
            if submitted is None:
                return {
                    "groups": [
                        _global_general_group(global_data),
                        *_backup_defaults_groups(
                            backup_defaults,
                            None,
                            fields=GLOBAL_BACKUP_FIELDS,
                            prefix="backup__",
                        ),
                        *_rclone_defaults_groups(
                            rclone,
                            None,
                            fields=GLOBAL_RCLONE_FIELDS,
                            prefix="rclone__",
                        ),
                        _global_notifications_group(global_data, notifications),
                    ],
                    "credential": _credential_view(backup_defaults),
                    "providers": [
                        _provider("mail", "Mail Provider", mail, MAIL_FIELDS),
                        _provider("pushover", "Pushover Provider", pushover, PUSHOVER_FIELDS),
                    ],
                    "error": None,
                }
        except Exception as exc:
            if submitted is None:
                return {"groups": [], "providers": [], "credential": {}, "error": str(exc)}
            password_configured = False

        assert submitted is not None
        return {
            "groups": [
                _global_general_group(
                    pseudo_table_from_form(GLOBAL_FIELDS, submitted, prefix="global__")
                ),
                *_backup_defaults_groups(
                    pseudo_table_from_form(GLOBAL_BACKUP_FIELDS, submitted, prefix="backup__"),
                    None,
                    fields=GLOBAL_BACKUP_FIELDS,
                    prefix="backup__",
                ),
                *_rclone_defaults_groups(
                    pseudo_table_from_form(GLOBAL_RCLONE_FIELDS, submitted, prefix="rclone__"),
                    None,
                    fields=GLOBAL_RCLONE_FIELDS,
                    prefix="rclone__",
                ),
                _global_notifications_group(
                    pseudo_table_from_form(GLOBAL_FIELDS, submitted, prefix="global__"),
                    pseudo_table_from_form(
                        GLOBAL_NOTIFICATION_FIELDS, submitted, prefix="notifications__"
                    ),
                ),
            ],
            "credential": _credential_view_from_form(
                submitted, password_configured=password_configured
            ),
            "providers": [
                _provider_from_form("mail", "Mail Provider", MAIL_FIELDS, submitted),
                _provider_from_form("pushover", "Pushover Provider", PUSHOVER_FIELDS, submitted),
            ],
            "error": None,
        }

    @_serialized_write
    def _save_global_form_sync(self, form: dict[str, str]) -> dict[str, Any]:
        """Save global settings submitted by the schema-driven form."""
        try:
            parsed = self._parse_raw()
            global_data = _get_or_create_table(parsed, "global", "global")
            apply_fields(global_data, GLOBAL_FIELDS, form, prefix="global__")
            backup_defaults = _apply_optional_child_fields(
                global_data,
                "backup",
                GLOBAL_BACKUP_FIELDS,
                form,
                prefix="backup__",
                context="global.backup",
                force=_credential_requested(form),
            )
            if backup_defaults is None:
                backup_defaults = _get_or_create_table(global_data, "backup", "global.backup")
            _apply_credential(backup_defaults, form)
            if not backup_defaults:
                global_data.pop("backup", None)
            _apply_optional_child_fields(
                global_data,
                "rclone",
                GLOBAL_RCLONE_FIELDS,
                form,
                prefix="rclone__",
                context="global.rclone",
            )
            notifications = _apply_optional_child_fields(
                global_data,
                "notifications",
                GLOBAL_NOTIFICATION_FIELDS,
                form,
                prefix="notifications__",
                context="global.notifications",
                force=_providers_requested(form),
            )
            if notifications is not None:
                _apply_provider(notifications, "mail", MAIL_FIELDS, form)
                _apply_provider(notifications, "pushover", PUSHOVER_FIELDS, form)
                if not notifications:
                    global_data.pop("notifications", None)
            return self._serialise_and_save(parsed)
        except (TypeError, ValueError) as exc:
            return {"saved": False, "error": str(exc)}

    async def save_global_form(self, form: dict[str, str]) -> dict[str, Any]:
        """Save global settings submitted by the schema-driven form."""
        return await asyncio.to_thread(self._save_global_form_sync, form)

    def _test_notification_provider_sync(
        self, provider: str, form: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Send a test notification through one configured provider."""
        if provider not in {"mail", "pushover"}:
            return {"ok": False, "provider": provider, "message": "Unknown notification provider"}

        try:
            timeout = env_timeout(
                NotificationDispatcher.TIMEOUT_ENV, NotificationDispatcher.DEFAULT_TIMEOUT, logger
            )
            event = _test_notification_event(provider)

            if provider == "mail":
                mail = (
                    _mail_provider_from_form(form)
                    if form is not None
                    else self._cfg.load_active_config().global_.notifications.mail
                )
                if mail is None:
                    return {
                        "ok": False,
                        "provider": provider,
                        "message": "Mail provider is not configured.",
                    }
                MailProvider(mail, timeout).send(event)
            else:
                pushover = (
                    _pushover_provider_from_form(form)
                    if form is not None
                    else self._cfg.load_active_config().global_.notifications.pushover
                )
                if pushover is None:
                    return {
                        "ok": False,
                        "provider": provider,
                        "message": "Pushover provider is not configured.",
                    }
                PushoverProvider(pushover, timeout).send(event)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "provider": provider,
                "message": f"Test notification failed: {exc}",
            }

        return {
            "ok": True,
            "provider": provider,
            "message": "Test notification sent successfully.",
        }

    async def test_notification_provider(
        self, provider: str, form: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Send a test notification through one configured provider."""
        return await asyncio.to_thread(self._test_notification_provider_sync, provider, form)

    def _test_notification_report_sync(
        self, provider: str, form: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Send a synthetic periodic report through one configured provider."""
        if provider not in {"mail", "pushover"}:
            return {"ok": False, "provider": provider, "message": "Unknown notification provider"}

        try:
            timeout = env_timeout(
                NotificationDispatcher.TIMEOUT_ENV, NotificationDispatcher.DEFAULT_TIMEOUT, logger
            )
            event = _test_report_event()

            if provider == "mail":
                mail = (
                    _mail_provider_from_form(form)
                    if form is not None
                    else self._cfg.load_active_config().global_.notifications.mail
                )
                if mail is None:
                    return {
                        "ok": False,
                        "provider": provider,
                        "message": "Mail provider is not configured.",
                    }
                MailProvider(mail, timeout).send_report(event)
            else:
                pushover = (
                    _pushover_provider_from_form(form)
                    if form is not None
                    else self._cfg.load_active_config().global_.notifications.pushover
                )
                if pushover is None:
                    return {
                        "ok": False,
                        "provider": provider,
                        "message": "Pushover provider is not configured.",
                    }
                PushoverProvider(pushover, timeout).send_report(event)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "provider": provider,
                "message": f"Test report failed: {exc}",
            }

        return {
            "ok": True,
            "provider": provider,
            "message": "Test report sent successfully.",
        }

    async def test_notification_report(
        self, provider: str, form: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Send a synthetic periodic report through one configured provider."""
        return await asyncio.to_thread(self._test_notification_report_sync, provider, form)

    @_serialized_write
    def _save_job_form_sync(self, *, job_name: str | None, form: dict[str, str]) -> dict[str, Any]:
        """Save the cross-cutting fields and hooks of a job form.

        Only the scalar cross-cutting keys (``hook_timeout``, ``notify_on_*``)
        and the job hook lists are mutated. The mixed ``backup``/``rclone``
        container subtables and workflow subtables are left untouched.
        """
        name = form.get("name", "").strip()
        if err := _validate_name(name, "job"):
            return {"saved": False, "error": err}

        try:
            parsed = self._parse_raw()
        except Exception as exc:
            return {"saved": False, "error": str(exc)}

        jobs_raw = _get_or_create_table(parsed, "jobs", "jobs")

        is_new = job_name is None
        if is_new:
            if name in jobs_raw:
                return {"saved": False, "error": f"Job {name!r} already exists"}
            jobs_raw[name] = tomlkit.table()
        else:
            assert job_name is not None
            if job_name not in jobs_raw:
                return {"saved": False, "error": f"Job not found: {job_name!r}"}
            if name != job_name and name in jobs_raw:
                return {"saved": False, "error": f"Job {name!r} already exists"}
            if name != job_name:
                jobs_raw[name] = jobs_raw.pop(job_name)

        job_data = _as_mapping(jobs_raw[name], f"job {name!r}")
        try:
            apply_fields(job_data, JOB_FIELDS, form)
        except ValueError as exc:
            return {"saved": False, "error": str(exc)}

        return self._serialise_and_save(parsed)

    async def save_job_form(self, *, job_name: str | None, form: dict[str, str]) -> dict[str, Any]:
        """Save the cross-cutting fields and hooks of a job form."""
        return await asyncio.to_thread(self._save_job_form_sync, job_name=job_name, form=form)

    @_serialized_write
    def _save_backup_defaults_form_sync(
        self, *, job_name: str, form: dict[str, str]
    ) -> dict[str, Any]:
        """Save the scalar default fields of ``[jobs.<job>.backup]``.

        Mixed-section protection: only the recognised scalar default keys and
        the credential keys of the container table are mutated. The sibling
        backup task subtables ``[jobs.<job>.backup.<name>]`` are never touched.
        """
        try:
            parsed = self._parse_raw()
        except Exception as exc:
            return {"saved": False, "error": str(exc)}

        jobs_raw = _mapping_or_empty(parsed, "jobs", "jobs")
        if job_name not in jobs_raw:
            return {"saved": False, "error": f"Job not found: {job_name!r}"}

        job_data = _as_mapping(jobs_raw[job_name], f"job {job_name!r}")
        backup_defaults = _get_or_create_table(job_data, "backup", f"job {job_name!r}.backup")
        try:
            apply_fields(backup_defaults, JOB_BACKUP_DEFAULTS_FIELDS, form)
            _apply_credential(backup_defaults, form, inheritable=True)
        except ValueError as exc:
            return {"saved": False, "error": str(exc)}

        if not backup_defaults:
            job_data.pop("backup", None)
        return self._serialise_and_save(parsed)

    async def save_backup_defaults_form(
        self, *, job_name: str, form: dict[str, str]
    ) -> dict[str, Any]:
        """Save the scalar default fields of ``[jobs.<job>.backup]``."""
        return await asyncio.to_thread(
            self._save_backup_defaults_form_sync, job_name=job_name, form=form
        )

    @_serialized_write
    def _save_rclone_defaults_form_sync(
        self, *, job_name: str, form: dict[str, str]
    ) -> dict[str, Any]:
        """Save the scalar default fields of ``[jobs.<job>.rclone]``.

        Mixed-section protection: only the recognised scalar default keys of the
        container table are mutated. The sibling rclone task subtables
        ``[jobs.<job>.rclone.<name>]`` are never touched.
        """
        try:
            parsed = self._parse_raw()
        except Exception as exc:
            return {"saved": False, "error": str(exc)}

        jobs_raw = _mapping_or_empty(parsed, "jobs", "jobs")
        if job_name not in jobs_raw:
            return {"saved": False, "error": f"Job not found: {job_name!r}"}

        job_data = _as_mapping(jobs_raw[job_name], f"job {job_name!r}")
        rclone_defaults = _get_or_create_table(job_data, "rclone", f"job {job_name!r}.rclone")
        try:
            apply_fields(rclone_defaults, JOB_RCLONE_DEFAULTS_FIELDS, form)
        except ValueError as exc:
            return {"saved": False, "error": str(exc)}

        if not rclone_defaults:
            job_data.pop("rclone", None)
        return self._serialise_and_save(parsed)

    async def save_rclone_defaults_form(
        self, *, job_name: str, form: dict[str, str]
    ) -> dict[str, Any]:
        """Save the scalar default fields of ``[jobs.<job>.rclone]``."""
        return await asyncio.to_thread(
            self._save_rclone_defaults_form_sync, job_name=job_name, form=form
        )

    @_serialized_write
    def _save_backup_form_sync(
        self, *, job_name: str, backup_name: str | None, form: dict[str, str]
    ) -> dict[str, Any]:
        """Save all editable fields of a backup task in a single TOML transaction.

        Mixed-section protection: only the addressed task subtable
        ``[jobs.<job>.backup.<name>]`` is mutated. The container's scalar
        defaults stay untouched because only the task subtable is written.
        """
        name = form.get("name", "").strip()
        repository = form.get("repository", "").strip()
        if err := _validate_name(name, "backup", _BACKUP_DEFAULT_FIELD_KEYS):
            return {"saved": False, "error": err}
        if not repository:
            return {"saved": False, "error": "repository must not be empty"}

        try:
            parsed = self._parse_raw()
        except Exception as exc:
            return {"saved": False, "error": str(exc)}

        jobs_raw = _mapping_or_empty(parsed, "jobs", "jobs")
        if job_name not in jobs_raw:
            return {"saved": False, "error": f"Job not found: {job_name!r}"}

        job_data = _as_mapping(jobs_raw[job_name], f"job {job_name!r}")
        backups_raw = _get_or_create_table(job_data, "backup", "backups")

        is_new = backup_name is None
        if is_new:
            if name in backups_raw:
                return {
                    "saved": False,
                    "error": f"Backup {name!r} already exists in job {job_name!r}",
                }
            backups_raw[name] = tomlkit.table()
        else:
            assert backup_name is not None
            backup_data = _backup_task_table(backups_raw, backup_name)
            if backup_data is None:
                return {"saved": False, "error": f"Backup not found: {backup_name!r}"}
            if name != backup_name and name in backups_raw:
                return {
                    "saved": False,
                    "error": f"Backup {name!r} already exists in job {job_name!r}",
                }
            if name != backup_name:
                backups_raw[name] = backups_raw.pop(backup_name)
                backup_data = _backup_task_table(backups_raw, name)

        backup_data = _backup_task_table(backups_raw, name)
        if backup_data is None:
            return {"saved": False, "error": f"Backup not found: {backup_name or name!r}"}
        backup_data["repository"] = repository
        try:
            apply_fields(backup_data, BACKUP_FIELDS, form)
            _apply_credential(backup_data, form, inheritable=True)
        except ValueError as exc:
            return {"saved": False, "error": str(exc)}

        sources = list(backup_data.get("sources", []))
        if err := _validate_sources(sources):
            return {"saved": False, "error": err}

        return self._serialise_and_save(parsed)

    async def save_backup_form(
        self, *, job_name: str, backup_name: str | None, form: dict[str, str]
    ) -> dict[str, Any]:
        """Save all editable fields of a backup task in a single TOML transaction."""
        return await asyncio.to_thread(
            self._save_backup_form_sync,
            job_name=job_name,
            backup_name=backup_name,
            form=form,
        )

    @_serialized_write
    def _save_workflow_form_sync(
        self, *, job_name: str, workflow_name: str | None, form: dict[str, str]
    ) -> dict[str, Any]:
        """Save all editable fields of a workflow form in a single TOML transaction."""
        name = form.get("name", "").strip()
        steps = _lines(form.get("steps", ""))
        if err := _validate_name(name, "workflow"):
            return {"saved": False, "error": err}
        if not steps:
            return {"saved": False, "error": "Workflow must have at least one step"}

        try:
            parsed = self._parse_raw()
        except Exception as exc:
            return {"saved": False, "error": str(exc)}

        jobs_raw = _mapping_or_empty(parsed, "jobs", "jobs")
        if job_name not in jobs_raw:
            return {"saved": False, "error": f"Job not found: {job_name!r}"}

        job_data = _as_mapping(jobs_raw[job_name], f"job {job_name!r}")
        backup_names = set(_task_names(job_data, "backup"))
        rclone_names = set(_task_names(job_data, "rclone"))

        if error := _validate_workflow_steps(steps, backup_names, rclone_names):
            return {"saved": False, "error": error}

        workflows_raw = _get_or_create_table(job_data, "workflow", "workflow")

        is_new = workflow_name is None
        if is_new:
            if name in workflows_raw:
                return {
                    "saved": False,
                    "error": f"Workflow {name!r} already exists in job {job_name!r}",
                }
            workflows_raw[name] = tomlkit.table()
        else:
            assert workflow_name is not None
            if workflow_name not in workflows_raw:
                return {"saved": False, "error": f"Workflow not found: {workflow_name!r}"}
            if name != workflow_name and name in workflows_raw:
                return {
                    "saved": False,
                    "error": f"Workflow {name!r} already exists in job {job_name!r}",
                }
            if name != workflow_name:
                workflows_raw[name] = workflows_raw.pop(workflow_name)

        wf_data = _as_mapping(workflows_raw[name], f"workflow {name!r}")
        wf_data["steps"] = steps
        try:
            apply_fields(wf_data, WORKFLOW_FIELDS, form)
        except ValueError as exc:
            return {"saved": False, "error": str(exc)}

        return self._serialise_and_save(parsed)

    async def save_workflow_form(
        self, *, job_name: str, workflow_name: str | None, form: dict[str, str]
    ) -> dict[str, Any]:
        """Save all editable fields of a workflow form in a single TOML transaction."""
        return await asyncio.to_thread(
            self._save_workflow_form_sync,
            job_name=job_name,
            workflow_name=workflow_name,
            form=form,
        )

    @_serialized_write
    def _save_rclone_form_sync(
        self, *, job_name: str, rclone_name: str | None, form: dict[str, str]
    ) -> dict[str, Any]:
        """Save all editable fields of an rclone task form in a single TOML transaction.

        Mixed-section protection: only the addressed task subtable
        ``[jobs.<job>.rclone.<name>]`` is mutated; the container's scalar
        defaults are left untouched.
        """
        name = form.get("name", "").strip()
        if err := _validate_name(name, "rclone task", _RCLONE_DEFAULT_FIELD_KEYS):
            return {"saved": False, "error": err}
        source = form.get("source", "").strip()
        target = form.get("target", "").strip()
        if not source:
            return {"saved": False, "error": "source must not be empty"}
        if not target:
            return {"saved": False, "error": "target must not be empty"}

        try:
            parsed = self._parse_raw()
        except Exception as exc:
            return {"saved": False, "error": str(exc)}

        jobs_raw = _mapping_or_empty(parsed, "jobs", "jobs")
        if job_name not in jobs_raw:
            return {"saved": False, "error": f"Job not found: {job_name!r}"}

        job_data = _as_mapping(jobs_raw[job_name], f"job {job_name!r}")
        rclone_table = _get_or_create_table(job_data, "rclone", "rclone tasks")

        if rclone_name is None:
            if name in rclone_table:
                return {
                    "saved": False,
                    "error": f"Rclone task {name!r} already exists in job {job_name!r}",
                }
            rclone_table[name] = tomlkit.table()
        else:
            task_data = _rclone_task_table(rclone_table, rclone_name)
            if task_data is None:
                return {"saved": False, "error": f"Rclone task not found: {rclone_name!r}"}
            if name != rclone_name and name in rclone_table:
                return {
                    "saved": False,
                    "error": f"Rclone task {name!r} already exists in job {job_name!r}",
                }
            if name != rclone_name:
                rclone_table[name] = rclone_table.pop(rclone_name)
                task_data = _rclone_task_table(rclone_table, name)

        task_data = _rclone_task_table(rclone_table, name)
        if task_data is None:
            return {"saved": False, "error": f"Rclone task not found: {rclone_name or name!r}"}
        try:
            apply_fields(task_data, RCLONE_FIELDS, form)
        except ValueError as exc:
            return {"saved": False, "error": str(exc)}

        return self._serialise_and_save(parsed)

    async def save_rclone_form(
        self, *, job_name: str, rclone_name: str | None, form: dict[str, str]
    ) -> dict[str, Any]:
        """Save all editable fields of an rclone task form in a single TOML transaction."""
        return await asyncio.to_thread(
            self._save_rclone_form_sync,
            job_name=job_name,
            rclone_name=rclone_name,
            form=form,
        )

    async def get_overview(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_overview_sync)

    def _get_overview_sync(self) -> dict[str, Any]:
        """Return a structural summary of all jobs.

        Parses raw TOML without Pydantic validation so the overview is
        available even when the config has validation errors. Per-resource
        display details (sources, repository, rclone endpoints, workflow
        steps) come straight from the resource tables — these fields are
        task-only in the config schema (``inheritance="none"``), so no
        default/inheritance lookup applies.

        Returns:
            Dict with keys:
                jobs: list of dicts with name, backup_names, workflow_names,
                    rclone_names, per-name validity maps and per-name detail
                    dicts (backup_details, workflow_details, rclone_details).
                error: str or None.
                active_config_error: str or None; set when the file parses but
                    the full loading pipeline fails, i.e. the active config is
                    not runnable.
        """
        try:
            raw = self._cfg._read_raw_sync()
            content = raw.get("content", "")
            warnings = raw.get("warnings", [])
            if not content:
                err = raw.get("error")
                if err:
                    msg = err["message"] if isinstance(err, dict) else str(err)
                    return {"jobs": [], "error": msg, "warnings": warnings}
                return {"jobs": [], "error": None, "warnings": warnings}
            active_config_error: str | None = None
            if not raw.get("valid", False):
                err = raw.get("error")
                if isinstance(err, dict):
                    active_config_error = str(err.get("message") or "")
                elif err:
                    active_config_error = str(err)
                active_config_error = active_config_error or "Invalid configuration"
            parsed = tomlkit.parse(str(content))
            jobs_raw = _mapping_or_empty(parsed, "jobs", "jobs")
            jobs: list[dict[str, Any]] = []
            for job_name, job_data in jobs_raw.items():
                if not isinstance(job_data, MutableMapping):
                    continue
                job_mapping = cast(TomlMapping, job_data)
                backup_names = list(_task_names(job_mapping, "backup"))
                workflow_names = list(_mapping_or_empty(job_mapping, "workflow", "workflow").keys())
                rclone_names = list(_task_names(job_mapping, "rclone"))
                backups = _mapping_or_empty(job_mapping, "backup", "backup")
                workflows = _mapping_or_empty(job_mapping, "workflow", "workflow")
                rclone_tasks = _mapping_or_empty(job_mapping, "rclone", "rclone")
                jobs.append(
                    {
                        "name": job_name,
                        "name_valid": _validate_name(str(job_name), "job") is None,
                        "backup_names": backup_names,
                        "backup_name_valid": {
                            name: _validate_name(str(name), "backup") is None
                            for name in backup_names
                        },
                        "backup_details": {
                            name: _backup_display_details(backups.get(name))
                            for name in backup_names
                        },
                        "workflow_names": workflow_names,
                        "workflow_name_valid": {
                            name: _validate_name(str(name), "workflow") is None
                            for name in workflow_names
                        },
                        "workflow_details": {
                            name: _workflow_display_details(workflows.get(name))
                            for name in workflow_names
                        },
                        "rclone_names": rclone_names,
                        "rclone_name_valid": {
                            name: _validate_name(str(name), "rclone task") is None
                            for name in rclone_names
                        },
                        "rclone_details": {
                            name: _rclone_display_details(rclone_tasks.get(name))
                            for name in rclone_names
                        },
                    }
                )
            return {
                "jobs": jobs,
                "error": None,
                "warnings": warnings,
                "active_config_error": active_config_error,
            }
        except (ConfigServiceError, FileNotFoundError) as exc:
            return {"jobs": [], "error": str(exc), "warnings": []}
        except Exception as exc:
            return {"jobs": [], "error": str(exc), "warnings": []}

    async def get_job_form(
        self,
        job_name: str | None = None,
        submitted: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._get_job_form_sync, job_name=job_name, submitted=submitted
        )

    def _get_job_form_sync(
        self,
        job_name: str | None = None,
        submitted: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Return the current cross-cutting field values for a job edit form.

        Args:
            job_name: Existing job name to load, or None for a blank new-job form.
            submitted: When provided, field values are taken from this dict instead of
                from the saved TOML, so user input is preserved after a failed save.

        Returns:
            Dict with keys: name, groups, error.
        """
        empty: dict[str, Any] = {"name": "", "error": None}
        try:
            parsed = self._parse_raw()
            parent: TomlMapping = _global_cross_cutting_parent(parsed)
        except Exception as exc:
            if submitted is None:
                return {**empty, "error": str(exc), "groups": _job_groups({}, None)}
            parent = {}

        if submitted is not None:
            table = pseudo_table_from_form(JOB_FIELDS, submitted)
            return {
                **empty,
                "name": submitted.get("name", ""),
                "groups": _job_groups(table, parent),
            }

        if job_name is None:
            return {**empty, "groups": _job_groups({}, parent)}

        jobs_raw = _mapping_or_empty(parsed, "jobs", "jobs")
        if job_name not in jobs_raw:
            return {**empty, "not_found": True, "error": f"Job not found: {job_name!r}"}

        job_data = _as_mapping(jobs_raw[job_name], f"job {job_name!r}")
        return {
            "name": job_name,
            "groups": _job_groups(job_data, parent),
            "error": None,
        }

    async def get_backup_defaults_form(
        self, job_name: str, submitted: dict[str, str] | None = None
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_backup_defaults_form_sync, job_name, submitted)

    def _get_backup_defaults_form_sync(
        self, job_name: str, submitted: dict[str, str] | None = None
    ) -> dict[str, Any]:
        empty: dict[str, Any] = {"job_name": job_name, "error": None}
        try:
            parsed = self._parse_raw()
            jobs_raw = _mapping_or_empty(parsed, "jobs", "jobs")
            if job_name not in jobs_raw:
                if submitted is None:
                    return {**empty, "not_found": True, "error": f"Job not found: {job_name!r}"}
                parent: TomlMapping = {}
                password_configured = False
            else:
                job_data = _as_mapping(jobs_raw[job_name], f"job {job_name!r}")
                defaults = _scalar_defaults_only(
                    _mapping_or_empty(job_data, "backup", "backup defaults"),
                    JOB_BACKUP_DEFAULTS_FIELDS,
                )
                parent = _global_backup_parent(parsed)
                password_configured = "password" in defaults
        except Exception as exc:
            if submitted is None:
                return {
                    **empty,
                    "error": str(exc),
                    "groups": _backup_defaults_groups({}, None),
                    "credential": _credential_view({}, inheritable=True),
                }
            parent = {}
            password_configured = False

        if submitted is not None:
            table = pseudo_table_from_form(JOB_BACKUP_DEFAULTS_FIELDS, submitted)
            return {
                **empty,
                "groups": _backup_defaults_groups(table, parent),
                "credential": _credential_view_from_form(
                    submitted,
                    parent=parent,
                    inheritable=True,
                    password_configured=password_configured,
                ),
            }

        if job_name not in jobs_raw:
            return {**empty, "not_found": True, "error": f"Job not found: {job_name!r}"}

        return {
            **empty,
            "groups": _backup_defaults_groups(defaults, parent),
            "credential": _credential_view(defaults, parent=parent, inheritable=True),
        }

    async def get_rclone_defaults_form(
        self, job_name: str, submitted: dict[str, str] | None = None
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_rclone_defaults_form_sync, job_name, submitted)

    def _get_rclone_defaults_form_sync(
        self, job_name: str, submitted: dict[str, str] | None = None
    ) -> dict[str, Any]:
        empty: dict[str, Any] = {"job_name": job_name, "error": None}
        try:
            parsed = self._parse_raw()
            jobs_raw = _mapping_or_empty(parsed, "jobs", "jobs")
            if job_name not in jobs_raw:
                if submitted is None:
                    return {**empty, "not_found": True, "error": f"Job not found: {job_name!r}"}
                parent: TomlMapping = {}
            else:
                job_data = _as_mapping(jobs_raw[job_name], f"job {job_name!r}")
                defaults = _scalar_defaults_only(
                    _mapping_or_empty(job_data, "rclone", "rclone defaults"),
                    JOB_RCLONE_DEFAULTS_FIELDS,
                )
                parent = _global_rclone_parent(parsed)
        except Exception as exc:
            if submitted is None:
                return {
                    **empty,
                    "error": str(exc),
                    "groups": _rclone_defaults_groups({}, None),
                }
            parent = {}

        if submitted is not None:
            table = pseudo_table_from_form(JOB_RCLONE_DEFAULTS_FIELDS, submitted)
            return {**empty, "groups": _rclone_defaults_groups(table, parent)}

        if job_name not in jobs_raw:
            return {**empty, "not_found": True, "error": f"Job not found: {job_name!r}"}

        return {**empty, "groups": _rclone_defaults_groups(defaults, parent)}

    async def get_backup_form(
        self,
        job_name: str,
        backup_name: str | None = None,
        submitted: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._get_backup_form_sync,
            job_name=job_name,
            backup_name=backup_name,
            submitted=submitted,
        )

    def _get_backup_form_sync(
        self,
        job_name: str,
        backup_name: str | None = None,
        submitted: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Return the current field values for a backup edit form.

        Args:
            job_name: Name of the parent job.
            backup_name: Existing backup name to load, or None for a blank new-backup form.
            submitted: When provided, field values are taken from this dict instead of
                from the saved TOML, so user input is preserved after a failed save.

        Returns:
            Dict with keys: name, repository, credential, groups, error.
        """
        empty: dict[str, Any] = {
            "name": "",
            "repository": "",
            "error": None,
        }

        try:
            parsed = self._parse_raw()
            jobs_raw = _mapping_or_empty(parsed, "jobs", "jobs")
            if job_name not in jobs_raw:
                if submitted is None:
                    return {**empty, "not_found": True, "error": f"Job not found: {job_name!r}"}
                parent: TomlMapping = {}
                password_configured = False
            else:
                job_data = _as_mapping(jobs_raw[job_name], f"job {job_name!r}")
                parent = _backup_task_parent(parsed, job_data)
                password_configured = False
                if submitted is not None and backup_name is not None:
                    braw = _mapping_or_empty(job_data, "backup", "backups")
                    bd = _backup_task_table(braw, backup_name)
                    if bd is not None:
                        password_configured = "password" in bd
        except Exception as exc:
            if submitted is None:
                return {**empty, "error": str(exc)}
            parent = {}
            password_configured = False

        if submitted is not None:
            table = pseudo_table_from_form(BACKUP_FIELDS, submitted)
            return {
                **empty,
                "name": submitted.get("name", ""),
                "repository": submitted.get("repository", ""),
                "groups": _backup_groups(table, parent),
                "credential": _credential_view_from_form(
                    submitted,
                    parent=parent,
                    inheritable=True,
                    password_configured=password_configured,
                ),
            }

        if backup_name is None:
            return {
                **empty,
                "groups": _backup_groups({}, parent),
                "credential": _credential_view({}, parent=parent, inheritable=True),
            }

        backups_raw = _mapping_or_empty(job_data, "backup", "backups")
        p = _backup_task_table(backups_raw, backup_name)
        if p is None:
            return {**empty, "not_found": True, "error": f"Backup not found: {backup_name!r}"}

        return {
            "name": backup_name,
            "repository": p.get("repository", ""),
            "groups": _backup_groups(p, parent),
            "credential": _credential_view(p, parent=parent, inheritable=True),
            "error": None,
        }

    async def get_workflow_form(
        self,
        job_name: str,
        workflow_name: str | None = None,
        submitted: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._get_workflow_form_sync,
            job_name=job_name,
            workflow_name=workflow_name,
            submitted=submitted,
        )

    def _get_workflow_form_sync(
        self,
        job_name: str,
        workflow_name: str | None = None,
        submitted: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Return the current field values for a workflow edit form.

        Args:
            job_name: Parent job name.
            workflow_name: Existing workflow name, or None for a blank form.
            submitted: When provided, field values are taken from this dict instead of
                from the saved TOML, so user input is preserved after a failed save.

        Returns:
            Dict with keys: name, schedule, steps, available_steps, error.
            available_steps contains the backup and rclone task names of the job.
        """
        empty: dict[str, Any] = {
            "name": "",
            "schedule": "",
            "steps": [],
            "step_rows": [],
            "available_steps": [],
            "available_backups": [],
            "available_rclone_tasks": [],
            "error": None,
        }

        try:
            parsed = self._parse_raw()
            jobs_raw = _mapping_or_empty(parsed, "jobs", "jobs")
            if job_name not in jobs_raw:
                if submitted is None:
                    return {**empty, "not_found": True, "error": f"Job not found: {job_name!r}"}
                available_steps: list[str] = []
                backup_names: list[str] = []
                rclone_names: list[str] = []
                parent: TomlMapping = {}
            else:
                job_data = _as_mapping(jobs_raw[job_name], f"job {job_name!r}")
                backup_names = list(_task_names(job_data, "backup"))
                rclone_names = list(_task_names(job_data, "rclone"))
                available_steps = [f"backup.{n}" for n in backup_names] + [
                    f"rclone.{n}" for n in rclone_names
                ]
                parent = _global_cross_cutting_parent(parsed)
                parent = cast(TomlMapping, effective_values(job_data, JOB_FIELDS, parent))
        except Exception as exc:
            if submitted is None:
                return {**empty, "error": str(exc)}
            available_steps = []
            backup_names = []
            rclone_names = []
            parent = {}

        if submitted is not None:
            table = pseudo_table_from_form(WORKFLOW_FIELDS, submitted)
            submitted_steps = _lines(submitted.get("steps", ""))
            return {
                **empty,
                "name": submitted.get("name", ""),
                "steps": submitted_steps,
                "step_rows": [_workflow_step_view(step) for step in submitted_steps],
                "available_steps": available_steps,
                "available_backups": backup_names,
                "available_rclone_tasks": rclone_names,
                "groups": _workflow_groups(table, parent),
            }

        if workflow_name is None:
            return {
                **empty,
                "available_steps": available_steps,
                "available_backups": backup_names,
                "available_rclone_tasks": rclone_names,
                "groups": _workflow_groups({}, parent),
            }

        workflows_raw = _mapping_or_empty(job_data, "workflow", "workflow")
        if workflow_name not in workflows_raw:
            return {
                **empty,
                "available_steps": available_steps,
                "not_found": True,
                "error": f"Workflow not found: {workflow_name!r}",
            }

        wf = _as_mapping(workflows_raw[workflow_name], f"workflow {workflow_name!r}")
        steps = list(wf.get("steps", []))
        return {
            "name": workflow_name,
            "schedule": wf.get("schedule", "") or "",
            "steps": steps,
            "step_rows": [_workflow_step_view(str(step)) for step in steps],
            "available_steps": available_steps,
            "available_backups": backup_names,
            "available_rclone_tasks": rclone_names,
            "groups": _workflow_groups(wf, parent),
            "error": None,
        }

    async def get_rclone_form(
        self,
        job_name: str,
        rclone_name: str | None = None,
        submitted: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._get_rclone_form_sync,
            job_name=job_name,
            rclone_name=rclone_name,
            submitted=submitted,
        )

    def _get_rclone_form_sync(
        self,
        job_name: str,
        rclone_name: str | None = None,
        submitted: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Return the current rclone task field values for a job.

        Args:
            job_name: The job whose rclone task to retrieve.
            rclone_name: Existing rclone task name to load, or None for a blank new-task form.
            submitted: When provided, field values are taken from this dict instead of
                from the saved TOML, so user input is preserved after a failed save.

        Returns:
            Dict with keys: source, target, sync_delete, groups, error.
        """
        empty: dict[str, Any] = {
            "name": "",
            "source": "",
            "target": "",
            "sync_delete": False,
            "primary_fields": field_views({}, _RCLONE_PRIMARY_FIELDS),
            "groups": [],
            "error": None,
        }

        try:
            parsed = self._parse_raw()
            jobs_raw = _mapping_or_empty(parsed, "jobs", "jobs")
            if job_name not in jobs_raw:
                if submitted is None:
                    return {**empty, "not_found": True, "error": f"Job not found: {job_name!r}"}
                parent: TomlMapping = {}
            else:
                job_data = _as_mapping(jobs_raw[job_name], f"job {job_name!r}")
                parent = _rclone_task_parent(parsed, job_data)
        except Exception as exc:
            if submitted is None:
                return {**empty, "error": str(exc)}
            parent = {}

        if submitted is not None:
            table = pseudo_table_from_form(RCLONE_FIELDS, submitted)
            return {
                **empty,
                "name": submitted.get("name", ""),
                "source": submitted.get("source", ""),
                "target": submitted.get("target", ""),
                "primary_fields": field_views(table, _RCLONE_PRIMARY_FIELDS),
                "groups": _rclone_groups(table, parent),
            }

        if rclone_name is None:
            return {
                **empty,
                "primary_fields": field_views({}, _RCLONE_PRIMARY_FIELDS),
                "groups": _rclone_groups({}, parent),
            }

        rclone_table = _mapping_or_empty(job_data, "rclone", "rclone tasks")
        task_data = _rclone_task_table(rclone_table, rclone_name)
        if task_data is None:
            return {**empty, "not_found": True, "error": f"Rclone task not found: {rclone_name!r}"}

        return {
            "name": rclone_name,
            "source": task_data.get("source", ""),
            "target": task_data.get("target", ""),
            "sync_delete": bool(task_data.get("sync_delete", False)),
            "primary_fields": field_views(task_data, _RCLONE_PRIMARY_FIELDS),
            "groups": _rclone_groups(task_data, parent),
            "error": None,
        }

    @_serialized_write
    def _delete_backup_sync(self, *, job_name: str, backup_name: str) -> dict[str, Any]:
        """Remove a backup task from a job.

        Mixed-section protection: only the addressed task subtable is removed;
        the container's scalar defaults stay intact.

        Args:
            job_name: Parent job name.
            backup_name: Name of the backup to delete.

        Returns:
            Dict with keys: saved (bool), error (str or None).
        """
        try:
            parsed = self._parse_raw()
            jobs_raw = _mapping_or_empty(parsed, "jobs", "jobs")
            if job_name not in jobs_raw:
                return {"saved": False, "error": f"Job not found: {job_name!r}"}
            job_data = _as_mapping(jobs_raw[job_name], f"job {job_name!r}")
            if "backup" not in job_data:
                return {"saved": False, "error": f"Backup not found: {backup_name!r}"}
            backups_raw = _as_mapping(job_data["backup"], "backups")
            if _backup_task_table(backups_raw, backup_name) is None:
                return {"saved": False, "error": f"Backup not found: {backup_name!r}"}
            del backups_raw[backup_name]
            if not backups_raw:
                del job_data["backup"]
            return self._serialise_and_save(parsed)
        except Exception as exc:
            return {"saved": False, "error": str(exc)}

    async def delete_backup(self, *, job_name: str, backup_name: str) -> dict[str, Any]:
        """Remove a backup task from a job."""
        return await asyncio.to_thread(
            self._delete_backup_sync, job_name=job_name, backup_name=backup_name
        )

    @_serialized_write
    def _delete_workflow_sync(self, *, job_name: str, workflow_name: str) -> dict[str, Any]:
        """Remove a workflow from a job.

        Args:
            job_name: Parent job name.
            workflow_name: Name of the workflow to delete.

        Returns:
            Dict with keys: saved (bool), error (str or None).
        """
        try:
            parsed = self._parse_raw()
            jobs_raw = _mapping_or_empty(parsed, "jobs", "jobs")
            if job_name not in jobs_raw:
                return {"saved": False, "error": f"Job not found: {job_name!r}"}
            job_data = _as_mapping(jobs_raw[job_name], f"job {job_name!r}")
            if "workflow" not in job_data:
                return {"saved": False, "error": f"Workflow not found: {workflow_name!r}"}
            workflows_raw = _as_mapping(job_data["workflow"], "workflow")
            if workflow_name not in workflows_raw:
                return {"saved": False, "error": f"Workflow not found: {workflow_name!r}"}
            del workflows_raw[workflow_name]
            if not workflows_raw:
                del job_data["workflow"]
            return self._serialise_and_save(parsed)
        except Exception as exc:
            return {"saved": False, "error": str(exc)}

    async def delete_workflow(self, *, job_name: str, workflow_name: str) -> dict[str, Any]:
        """Remove a workflow from a job."""
        return await asyncio.to_thread(
            self._delete_workflow_sync, job_name=job_name, workflow_name=workflow_name
        )

    @_serialized_write
    def _delete_rclone_sync(self, *, job_name: str, rclone_name: str) -> dict[str, Any]:
        """Remove a named rclone task from a job.

        Mixed-section protection: only the addressed task subtable is removed;
        the container's scalar defaults stay intact.

        Args:
            job_name: Parent job name.
            rclone_name: Name of the rclone task to delete.

        Returns:
            Dict with keys: saved (bool), error (str or None).
        """
        try:
            parsed = self._parse_raw()
            jobs_raw = _mapping_or_empty(parsed, "jobs", "jobs")
            if job_name not in jobs_raw:
                return {"saved": False, "error": f"Job not found: {job_name!r}"}
            job_data = _as_mapping(jobs_raw[job_name], f"job {job_name!r}")
            if "rclone" not in job_data:
                return {"saved": False, "error": f"Rclone task not found: {rclone_name!r}"}
            rclone_table = _as_mapping(job_data["rclone"], "rclone tasks")
            if _rclone_task_table(rclone_table, rclone_name) is None:
                return {"saved": False, "error": f"Rclone task not found: {rclone_name!r}"}
            del rclone_table[rclone_name]
            if not rclone_table:
                del job_data["rclone"]
            return self._serialise_and_save(parsed)
        except Exception as exc:
            return {"saved": False, "error": str(exc)}

    async def delete_rclone(self, *, job_name: str, rclone_name: str) -> dict[str, Any]:
        """Remove a named rclone task from a job."""
        return await asyncio.to_thread(
            self._delete_rclone_sync, job_name=job_name, rclone_name=rclone_name
        )

    @_serialized_write
    def _delete_job_sync(self, *, job_name: str) -> dict[str, Any]:
        """Remove an entire job including all backups, workflows, and rclone tasks.

        Args:
            job_name: Name of the job to delete.

        Returns:
            Dict with keys: saved (bool), error (str or None).
        """
        try:
            parsed = self._parse_raw()
            jobs_raw = _mapping_or_empty(parsed, "jobs", "jobs")
            if job_name not in jobs_raw:
                return {"saved": False, "error": f"Job not found: {job_name!r}"}
            del jobs_raw[job_name]
            return self._serialise_and_save(parsed)
        except Exception as exc:
            return {"saved": False, "error": str(exc)}

    async def delete_job(self, *, job_name: str) -> dict[str, Any]:
        """Remove an entire job including all backups, workflows, and rclone tasks."""
        return await asyncio.to_thread(self._delete_job_sync, job_name=job_name)

    def _parse_raw(self) -> TOMLDocument:
        """Read and parse the raw TOML content.

        Does not run Pydantic validation so callers can work with partially
        invalid configs (e.g. missing sources, unset env vars). A missing
        config file is treated as an empty document so the editor can create
        the very first config from scratch (the file is written on save).

        Raises:
            ConfigServiceError: When the file exists but cannot be read.
            tomlkit.exceptions.ParseError: When the content is not valid TOML syntax.
            Exception: Any other unexpected error.
        """
        raw = self._cfg._read_raw_sync()
        content = raw.get("content", "")
        if not content:
            err = raw.get("error")
            if err:
                code = err.get("code") if isinstance(err, dict) else None
                if code == "read_error":
                    msg = err["message"] if isinstance(err, dict) else str(err)
                    raise ConfigServiceError("config_error", msg)
        return tomlkit.parse(str(content))

    def _serialise_and_save(self, data: TOMLDocument) -> dict[str, Any]:
        """Serialise a tomlkit document and persist it via ConfigService.

        Args:
            data: The full config document to write.

        Returns:
            Dict with keys: saved (bool), error (str or None).
        """
        try:
            toml_str = tomlkit.dumps(data)
        except Exception as exc:
            return {"saved": False, "error": f"Serialisation error: {exc}"}

        try:
            result = self._cfg._save_raw_sync(toml_str)
        except Exception as exc:
            return {"saved": False, "error": str(exc)}

        if result.get("valid"):
            return {"saved": True, "error": None, "warnings": result.get("warnings", [])}

        err = result.get("error")
        msg = err["message"] if isinstance(err, dict) else str(err)
        return {"saved": False, "error": self._attribute_validation_error(msg)}

    def _attribute_validation_error(self, message: str) -> str:
        """Mark validation failures that already exist in the file on disk.

        Every structured save validates the full config. When that fails, the
        current on-disk file is validated as well: if it fails with the same
        message, the edit is not the cause (the file was broken beforehand,
        e.g. by a manual edit) and the message points to the raw editor
        instead of blaming the submitted change.
        """
        try:
            current = self._cfg._read_raw_sync()
        except Exception:
            return message
        if current.get("valid"):
            return message
        err = current.get("error")
        current_msg = err["message"] if isinstance(err, dict) else str(err or "")
        if current_msg != message:
            return message
        return (
            "Not saved — the config file already fails validation independently of "
            f"your change. Fix the existing error in the raw editor first: {message}"
        )


# Mixed-section helpers: backup/rclone container tables hold scalar defaults
# plus nested task subtables under the SAME table. Task lookups must therefore
# distinguish task subtables from the scalar default keys.

_BACKUP_DEFAULT_FIELD_KEYS = {item.key for item in JOB_BACKUP_DEFAULTS_FIELDS} | {
    "password",
    "password_env",
    "password_file",
}
_RCLONE_DEFAULT_FIELD_KEYS = {item.key for item in JOB_RCLONE_DEFAULTS_FIELDS}


def _task_names(job: TomlMapping, kind: str) -> list[str]:
    container = _mapping_or_empty(job, kind, kind)
    default_keys = _BACKUP_DEFAULT_FIELD_KEYS if kind == "backup" else _RCLONE_DEFAULT_FIELD_KEYS
    names: list[str] = []
    for key, value in container.items():
        if key in default_keys:
            continue
        if isinstance(value, MutableMapping):
            names.append(str(key))
    return names


def _display_scalar(value: Any) -> str | None:
    if value is None or isinstance(value, MutableMapping | list):
        return None
    return str(value)


def _display_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if not isinstance(item, MutableMapping)]
    return []


def _backup_display_details(table: Any) -> dict[str, Any]:
    """Display details of one backup task (task-only fields, no inheritance)."""
    mapping = table if isinstance(table, MutableMapping) else {}
    return {
        "repository": _display_scalar(mapping.get("repository")),
        "sources": _display_list(mapping.get("sources")),
        "source_files": _display_list(mapping.get("source_files")),
    }


def _workflow_display_details(table: Any) -> dict[str, Any]:
    """Display details of one workflow (step chain)."""
    mapping = table if isinstance(table, MutableMapping) else {}
    return {"steps": _display_list(mapping.get("steps"))}


def _rclone_display_details(table: Any) -> dict[str, Any]:
    """Display details of one rclone task (source/target endpoints)."""
    mapping = table if isinstance(table, MutableMapping) else {}
    return {
        "source": _display_scalar(mapping.get("source")),
        "target": _display_scalar(mapping.get("target")),
    }


def _backup_task_table(backups: TomlMapping, name: str) -> TomlMapping | None:
    if name in _BACKUP_DEFAULT_FIELD_KEYS:
        return None
    value = backups.get(name)
    if not isinstance(value, MutableMapping):
        return None
    return cast(TomlMapping, value)


def _rclone_task_table(rclone: TomlMapping, name: str) -> TomlMapping | None:
    if name in _RCLONE_DEFAULT_FIELD_KEYS:
        return None
    value = rclone.get(name)
    if not isinstance(value, MutableMapping):
        return None
    return cast(TomlMapping, value)


def _form_has_values(
    fields: tuple[EditorField, ...], form: dict[str, str], *, prefix: str = ""
) -> bool:
    for item in fields:
        name = f"{prefix}{item.key}"
        if form.get(name, "").strip():
            return True
        if item.kind == "list" and item.inheritable:
            if form.get(f"{name}__empty", "").lower() in {"1", "true", "yes", "on"}:
                return True
            mode = form.get(f"{name}__mode")
            if mode == "empty":
                return True
    return False


def _providers_requested(form: dict[str, str]) -> bool:
    return form.get("mail__enabled") == "true" or form.get("pushover__enabled") == "true"


def _credential_requested(form: dict[str, str]) -> bool:
    return form.get("credential__mode") in {"password", "password_env", "password_file"}


def _apply_optional_child_fields(
    parent: TomlMapping,
    key: str,
    fields: tuple[EditorField, ...],
    form: dict[str, str],
    *,
    prefix: str,
    context: str,
    force: bool = False,
) -> TomlMapping | None:
    exists = key in parent
    if not exists and not force and not _form_has_values(fields, form, prefix=prefix):
        return None

    child = _get_or_create_table(parent, key, context)
    apply_fields(child, fields, form, prefix=prefix)
    if not child and not force:
        parent.pop(key, None)
        return None
    return child


def _group(
    title: str,
    table: TomlMapping,
    fields: tuple[EditorField, ...],
    *,
    parent: TomlMapping | None = None,
    prefix: str = "",
) -> dict[str, Any]:
    views = field_views(table, fields, parent=parent, prefix=prefix)
    return {
        "title": title,
        "fields": views,
        "open": any(field["required"] for field in views),
    }


def _global_general_group(table: TomlMapping) -> dict[str, Any]:
    fields = {f.key: f for f in GLOBAL_FIELDS}
    return _group(
        "General",
        table,
        tuple(
            fields[k]
            for k in (
                "log_level",
                "log_retention_days",
                "lock_retry_count",
                "lock_retry_delay",
                "hook_timeout",
            )
        ),
        prefix="global__",
    )


def _global_notifications_group(
    table: TomlMapping, notifications: TomlMapping | None = None
) -> dict[str, Any]:
    fields = {f.key: f for f in GLOBAL_FIELDS}
    group = _group(
        "Notifications",
        table,
        tuple(
            fields[k]
            for k in (
                "notify_on_success",
                "notify_on_error",
                "notify_on_skipped",
            )
        ),
        prefix="global__",
    )
    group["fields"].extend(
        field_views(notifications or {}, GLOBAL_NOTIFICATION_FIELDS, prefix="notifications__")
    )
    return group


def _backup_groups(table: TomlMapping, parent: TomlMapping | None) -> list[dict[str, Any]]:
    pf = {f.key: f for f in BACKUP_FIELDS}

    def g(title: str, *keys: str) -> dict[str, Any]:
        return _group(title, table, tuple(pf[k] for k in keys), parent=parent)

    return [
        g("Basic", "backend", "sources", "source_files", "schedule"),
        g("Repository Options", "auto_init"),
        g(
            "Backup-Task",
            "tags",
            "exclude",
            "exclude_files",
            "exclude_caches",
            "one_file_system",
            "backup_timeout",
            "extra_restic_backup_args",
        ),
        g(
            "Retention-Task",
            "retention",
            "keep_last",
            "keep_hourly",
            "keep_daily",
            "keep_weekly",
            "keep_monthly",
            "keep_yearly",
            "keep_within",
            "keep_within_hourly",
            "keep_within_daily",
            "keep_within_weekly",
            "keep_within_monthly",
            "keep_within_yearly",
            "extra_restic_forget_args",
        ),
        g(
            "Cleanup-Task",
            "cleanup",
            "extra_restic_prune_args",
        ),
        g("Hooks", "pre_hooks", "post_hooks", "on_error_hooks", "hook_timeout"),
        g(
            "Notifications",
            "notify_on_success",
            "notify_on_error",
            "notify_on_skipped",
        ),
    ]


def _backup_defaults_groups(
    table: TomlMapping,
    parent: TomlMapping | None,
    *,
    fields: tuple[EditorField, ...] = JOB_BACKUP_DEFAULTS_FIELDS,
    prefix: str = "",
) -> list[dict[str, Any]]:
    pf = {f.key: f for f in fields}

    def g(title: str, *keys: str) -> dict[str, Any]:
        return _group(title, table, tuple(pf[k] for k in keys), parent=parent, prefix=prefix)

    return [
        g("Repository Options", "auto_init"),
        g(
            "Backup-Task",
            "exclude",
            "exclude_files",
            "exclude_caches",
            "one_file_system",
            "backup_timeout",
            "extra_restic_backup_args",
        ),
        g(
            "Retention-Task",
            "retention",
            "keep_last",
            "keep_hourly",
            "keep_daily",
            "keep_weekly",
            "keep_monthly",
            "keep_yearly",
            "keep_within",
            "keep_within_hourly",
            "keep_within_daily",
            "keep_within_weekly",
            "keep_within_monthly",
            "keep_within_yearly",
            "extra_restic_forget_args",
        ),
        g(
            "Cleanup-Task",
            "cleanup",
            "extra_restic_prune_args",
        ),
    ]


def _rclone_defaults_groups(
    table: TomlMapping,
    parent: TomlMapping | None,
    *,
    fields: tuple[EditorField, ...] = JOB_RCLONE_DEFAULTS_FIELDS,
    prefix: str = "",
) -> list[dict[str, Any]]:
    pf = {f.key: f for f in fields}

    def g(title: str, *keys: str) -> dict[str, Any]:
        return _group(title, table, tuple(pf[k] for k in keys), parent=parent, prefix=prefix)

    return [
        g("Sync", "sync_delete", "exclude", "filter_from"),
        g(
            "Rclone Options",
            "transfers",
            "checkers",
            "bwlimit",
            "extra_rclone_args",
            "rclone_timeout",
        ),
    ]


def _workflow_groups(table: TomlMapping, parent: TomlMapping | None) -> list[dict[str, Any]]:
    pf = {f.key: f for f in WORKFLOW_FIELDS}

    def g(title: str, *keys: str) -> dict[str, Any]:
        return _group(title, table, tuple(pf[k] for k in keys), parent=parent)

    return [
        g("Basic", "schedule"),
        g("Hooks", "pre_hooks", "post_hooks", "on_error_hooks", "hook_timeout"),
        g(
            "Notifications",
            "notify_on_success",
            "notify_on_error",
            "notify_on_skipped",
        ),
    ]


def _rclone_groups(table: TomlMapping, parent: TomlMapping | None) -> list[dict[str, Any]]:
    pf = {f.key: f for f in _RCLONE_OPTION_FIELDS}

    def g(title: str, *keys: str) -> dict[str, Any]:
        return _group(title, table, tuple(pf[k] for k in keys), parent=parent)

    return [
        g("Basic", "sync_delete", "schedule"),
        g("Filter", "exclude", "filter_from"),
        g(
            "Rclone Options",
            "transfers",
            "checkers",
            "bwlimit",
            "rclone_timeout",
            "extra_rclone_args",
        ),
        g("Hooks", "pre_hooks", "post_hooks", "on_error_hooks", "hook_timeout"),
        g(
            "Notifications",
            "notify_on_success",
            "notify_on_error",
            "notify_on_skipped",
        ),
    ]


def _job_groups(table: TomlMapping, parent: TomlMapping | None) -> list[dict[str, Any]]:
    jf = {f.key: f for f in JOB_FIELDS}

    def g(title: str, *keys: str) -> dict[str, Any]:
        return _group(title, table, tuple(jf[k] for k in keys), parent=parent)

    return [
        g("Hooks", "pre_hooks", "post_hooks", "on_error_hooks", "hook_timeout"),
        g(
            "Notifications",
            "notify_on_success",
            "notify_on_error",
            "notify_on_skipped",
        ),
    ]


def _provider(
    name: str, title: str, table: TomlMapping, fields: tuple[EditorField, ...]
) -> dict[str, Any]:
    enabled = bool(table)
    display_table = table if enabled else _field_default_values(fields)
    return {
        "name": name,
        "title": title,
        "enabled": enabled,
        "groups": [_group(title, display_table, fields, prefix=f"{name}__")],
    }


def _provider_from_form(
    name: str, title: str, fields: tuple[EditorField, ...], form: dict[str, str]
) -> dict[str, Any]:
    table = pseudo_table_from_form(fields, form, prefix=f"{name}__")
    if form.get(f"{name}__enabled") == "true":
        table = {**_field_default_values(fields), **table}
    return {
        "name": name,
        "title": title,
        "enabled": form.get(f"{name}__enabled") == "true",
        "groups": [_group(title, table, fields, prefix=f"{name}__")],
    }


def _apply_provider(
    notifications: TomlMapping,
    name: str,
    fields: tuple[EditorField, ...],
    form: dict[str, str],
) -> None:
    enabled_key = f"{name}__enabled"
    if enabled_key not in form:
        return
    if form.get(enabled_key) != "true":
        notifications.pop(name, None)
        return
    provider = _get_or_create_table(notifications, name, f"global.notifications.{name}")
    apply_fields(provider, fields, _provider_form_defaults(name, fields, form), prefix=f"{name}__")


def _mail_provider_from_form(form: dict[str, str] | None) -> ResolvedMailNotificationConfig | None:
    table = _provider_table_from_form("mail", MAIL_FIELDS, form)
    if table is None:
        return None
    return ResolvedMailNotificationConfig.model_validate(table)


def _pushover_provider_from_form(
    form: dict[str, str] | None,
) -> ResolvedPushoverNotificationConfig | None:
    table = _provider_table_from_form("pushover", PUSHOVER_FIELDS, form)
    if table is None:
        return None
    return ResolvedPushoverNotificationConfig.model_validate(table)


def _provider_table_from_form(
    name: str, fields: tuple[EditorField, ...], form: dict[str, str] | None
) -> dict[str, Any] | None:
    """Parse one optional provider from submitted form values."""
    if form is None or form.get(f"{name}__enabled") != "true":
        return None
    table: dict[str, Any] = {}
    apply_fields(table, fields, _provider_form_defaults(name, fields, form), prefix=f"{name}__")
    return table


def _field_default_values(fields: tuple[EditorField, ...]) -> dict[str, str]:
    return {item.key: item.default_value for item in fields if item.default_value}


def _provider_form_defaults(
    name: str, fields: tuple[EditorField, ...], form: dict[str, str]
) -> dict[str, str]:
    """Fill missing provider form keys with schema defaults before saving."""
    values = dict(form)
    for item in fields:
        if item.default_value:
            values.setdefault(f"{name}__{item.key}", item.default_value)
    return values


def _credential_view(
    table: TomlMapping,
    *,
    parent: TomlMapping | None = None,
    inheritable: bool = False,
) -> dict[str, Any]:
    if "password_env" in table:
        mode = "password_env"
    elif "password_file" in table:
        mode = "password_file"
    elif "password" in table:
        mode = "password"
    else:
        mode = "inherit" if inheritable else "unset"
    parent_configured = bool(
        parent and ("password" in parent or "password_env" in parent or "password_file" in parent)
    )
    parent_hint = _credential_parent_hint(parent)
    modes = [("inherit", "Inherit")] if inheritable else [("unset", "Do not set")]
    modes.extend(
        [
            ("password_env", "Environment variable"),
            ("password_file", "Password file"),
            ("password", "Direct password"),
        ]
    )
    return {
        "mode": mode,
        "modes": modes,
        "password_env": table.get("password_env", "") or "",
        "password_file": table.get("password_file", "") or "",
        "password_configured": "password" in table,
        "parent_configured": parent_configured,
        "parent_hint": parent_hint,
        "password_reentry_required": False,
    }


def _credential_view_from_form(
    form: dict[str, str],
    *,
    parent: TomlMapping | None = None,
    inheritable: bool = False,
    password_configured: bool = False,
) -> dict[str, Any]:
    default = "inherit" if inheritable else "unset"
    mode = form.get("credential__mode", default)
    parent_configured = bool(
        parent and ("password" in parent or "password_env" in parent or "password_file" in parent)
    )
    parent_hint = _credential_parent_hint(parent)
    modes: list[tuple[str, str]] = (
        [("inherit", "Inherit")] if inheritable else [("unset", "Do not set")]
    )
    modes.extend(
        [
            ("password_env", "Environment variable"),
            ("password_file", "Password file"),
            ("password", "Direct password"),
        ]
    )
    return {
        "mode": mode,
        "modes": modes,
        "password_env": form.get("credential__password_env", "") or "",
        "password_file": form.get("credential__password_file", "") or "",
        "password_configured": password_configured,
        "parent_configured": parent_configured,
        "parent_hint": parent_hint,
        "password_reentry_required": mode == "password" and not password_configured,
    }


def _credential_parent_hint(parent: TomlMapping | None) -> str:
    """Describe inherited credentials without exposing direct passwords."""
    if not parent:
        return ""
    if "password_env" in parent:
        return f"Inherited environment variable: {parent['password_env']}"
    if "password_file" in parent:
        return f"Inherited password file: {parent['password_file']}"
    if "password" in parent:
        return "An inherited direct password is configured and is not disclosed."
    return ""


def _apply_credential(
    table: TomlMapping, form: dict[str, str], *, inheritable: bool = False
) -> None:
    default = "inherit" if inheritable else "unset"
    if "credential__mode" not in form:
        return
    mode = form.get("credential__mode", default)
    if mode in {"inherit", "unset"}:
        table.pop("password", None)
        table.pop("password_env", None)
        table.pop("password_file", None)
    elif mode == "password_env":
        value = form.get("credential__password_env", "").strip()
        if not value:
            raise ValueError("password_env must not be empty")
        table.pop("password", None)
        table.pop("password_file", None)
        table["password_env"] = value
    elif mode == "password_file":
        value = form.get("credential__password_file", "").strip()
        if not value:
            raise ValueError("password_file must not be empty")
        table.pop("password", None)
        table.pop("password_env", None)
        table["password_file"] = value
    elif mode == "password":
        value = form.get("credential__password", "")
        if value:
            table["password"] = value
        elif "password" not in table:
            raise ValueError("password must not be empty")
        table.pop("password_env", None)
        table.pop("password_file", None)


# Parent (inherited) namespaces for inheritance hints. Each child level has its
# own inheritance chain:
#   backup task:  global.backup -> jobs.<job>.backup -> backup,
#                 plus cross-cutting global -> job -> backup
#   rclone task:  global.rclone -> jobs.<job>.rclone -> rclone,
#                 plus cross-cutting global -> job -> rclone


def _global_cross_cutting_parent(parsed: TomlMapping) -> TomlMapping:
    """Flatten [global] cross-cutting defaults inherited by jobs."""
    global_data = _mapping_or_empty(parsed, "global", "global")
    defaults = RawGlobalConfig.model_validate({}).model_dump()
    result: TomlMapping = {
        key: value for key, value in defaults.items() if key.startswith("notify_on_")
    }
    for key in (
        "hook_timeout",
        "notify_on_success",
        "notify_on_error",
        "notify_on_skipped",
    ):
        if key in global_data:
            result[key] = global_data[key]
    return result


def _global_backup_parent(parsed: TomlMapping) -> TomlMapping:
    global_data = _mapping_or_empty(parsed, "global", "global")
    defaults = RawGlobalConfig.model_validate({}).model_dump()
    backup_defaults = cast(TomlMapping, defaults["backup"])
    result: TomlMapping = {
        "retention": backup_defaults["retention"],
        "cleanup": backup_defaults["cleanup"],
        "auto_init": backup_defaults["auto_init"],
    }
    result.update(_mapping_or_empty(global_data, "backup", "global.backup"))
    return result


def _global_rclone_parent(parsed: TomlMapping) -> TomlMapping:
    global_data = _mapping_or_empty(parsed, "global", "global")
    return dict(_mapping_or_empty(global_data, "rclone", "global.rclone"))


def _backup_task_parent(parsed: TomlMapping, job: TomlMapping) -> TomlMapping:
    """Flatten effective backup defaults inherited by a concrete backup task."""
    result = _global_backup_parent(parsed)
    job_backup = _scalar_defaults_only(
        _mapping_or_empty(job, "backup", "job backup defaults"),
        JOB_BACKUP_DEFAULTS_FIELDS,
    )
    result = cast(TomlMapping, effective_values(job_backup, JOB_BACKUP_DEFAULTS_FIELDS, result))
    _merge_password(result, job_backup)
    cross = _global_cross_cutting_parent(parsed)
    result.update(cast(TomlMapping, effective_values(job, JOB_FIELDS, cross)))
    return result


def _rclone_task_parent(parsed: TomlMapping, job: TomlMapping) -> TomlMapping:
    """Flatten effective rclone defaults inherited by a concrete rclone task."""
    result = _global_rclone_parent(parsed)
    job_rclone = _scalar_defaults_only(
        _mapping_or_empty(job, "rclone", "job rclone defaults"),
        JOB_RCLONE_DEFAULTS_FIELDS,
    )
    result = cast(TomlMapping, effective_values(job_rclone, JOB_RCLONE_DEFAULTS_FIELDS, result))
    cross = _global_cross_cutting_parent(parsed)
    result.update(cast(TomlMapping, effective_values(job, JOB_FIELDS, cross)))
    return result


def _merge_password(result: TomlMapping, table: TomlMapping) -> None:
    """Merge the active password choice of ``table`` into ``result``."""
    if "password" in table:
        result["password"] = table["password"]
        result.pop("password_env", None)
        result.pop("password_file", None)
    elif "password_env" in table:
        result["password_env"] = table["password_env"]
        result.pop("password", None)
        result.pop("password_file", None)
    elif "password_file" in table:
        result["password_file"] = table["password_file"]
        result.pop("password", None)
        result.pop("password_env", None)


def _lines(raw: str) -> list[str]:
    """Split textarea input into stripped non-empty lines."""
    return [line.strip() for line in raw.splitlines() if line.strip()]
