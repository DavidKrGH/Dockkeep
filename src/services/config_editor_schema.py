"""Schema-driven fields for the structured Raw-TOML config editor."""

from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any, Literal

from ..models.config_fields import ConfigField, fields_for_level

FieldKind = Literal["bool", "checklist", "list", "number", "select", "text"]
TomlMapping = MutableMapping[str, Any]


@dataclass(frozen=True)
class EditorField:
    """Describe one editable TOML field."""

    key: str
    label: str = ""
    kind: FieldKind = "text"
    cron: bool = False
    inheritable: bool = False
    optional: bool = True
    choices: tuple[str, ...] = ()
    placeholder: str = ""
    help_text: str = ""
    info_panels: tuple[dict[str, Any], ...] = ()
    parent_key: str | None = None
    default_hint: str = ""
    domain_field: ConfigField | None = None
    default_value: str = ""


def field(
    key: str,
    kind: FieldKind = "text",
    *,
    label: str = "",
    cron: bool = False,
    inheritable: bool = False,
    optional: bool = True,
    choices: tuple[str, ...] = (),
    placeholder: str = "",
    help_text: str = "",
    info_panels: tuple[dict[str, Any], ...] = (),
    parent_key: str | None = None,
    default_hint: str = "",
    default_value: str = "",
) -> EditorField:
    return EditorField(
        key=key,
        label=label,
        kind=kind,
        cron=cron,
        inheritable=inheritable,
        optional=optional,
        choices=choices,
        placeholder=placeholder,
        help_text=help_text,
        info_panels=info_panels,
        parent_key=parent_key,
        default_hint=default_hint,
        domain_field=None,
        default_value=default_value,
    )


_EDITOR_KIND_MAP: dict[str, FieldKind] = {
    "bool": "bool",
    "choice": "select",
    "cron": "text",
    "int": "number",
    "list": "list",
    "password": "text",
    "text": "text",
}

_LABELS: dict[str, str] = {
    "auto_init": "Auto-initialize repository",
    "backup_timeout": "Backup timeout (seconds)",
    "backend": "Backend",
    "bwlimit": "Bandwidth limit",
    "checkers": "Checker threads",
    "cleanup": "Run cleanup automatically",
    "exclude": "Exclude patterns",
    "exclude_caches": "Exclude cache directories",
    "exclude_files": "Exclude files",
    "extra_rclone_args": "Additional Rclone arguments",
    "extra_restic_backup_args": "Additional backup arguments",
    "extra_restic_forget_args": "Additional retention arguments",
    "extra_restic_prune_args": "Additional cleanup arguments",
    "filter_from": "Filter file",
    "hook_timeout": "Hook timeout (seconds)",
    "keep_daily": "Daily snapshots",
    "keep_hourly": "Hourly snapshots",
    "keep_last": "Keep last N",
    "keep_monthly": "Monthly snapshots",
    "keep_weekly": "Weekly snapshots",
    "keep_within": "Keep within",
    "keep_within_hourly": "Hourly within",
    "keep_within_daily": "Daily within",
    "keep_within_weekly": "Weekly within",
    "keep_within_monthly": "Monthly within",
    "keep_within_yearly": "Yearly within",
    "keep_yearly": "Yearly snapshots",
    "log_level": "Log-Level",
    "lock_retry_count": "Lock retry count",
    "lock_retry_delay": "Lock retry delay (seconds)",
    "log_retention_days": "Log retention (days)",
    "notify_on_error": "Notify on error",
    "notify_on_skipped": "Notify on skipped",
    "notify_on_success": "Notify on success",
    "one_file_system": "One file system only",
    "rclone_timeout": "Rclone timeout (seconds)",
    "retention": "Run retention automatically",
    "report_schedule": "Report schedule",
    "schedule": "Cron schedule",
    "source": "Source path",
    "source_files": "Source file lists",
    "sources": "Source paths",
    "sync_delete": "Delete during sync",
    "tags": "Tags",
    "target": "Rclone target",
    "transfers": "Parallel transfers",
    "pre_hooks": "Pre-Hooks",
    "post_hooks": "Post-Hooks",
    "on_error_hooks": "On-Error-Hooks",
}


def field_label(key: str) -> str:
    """Gibt das nutzersichtbare Label für einen Config-Feldnamen zurück.

    Args:
        key: Raw-Feldname aus dem Config-Schema (z.B. ``"keep_last"``).

    Returns:
        Das kuratierte englische Label, oder ``key`` unverändert, falls kein
        Label definiert ist.
    """
    return _LABELS.get(key, key)


_HELP_TEXTS: dict[str, str] = {
    "exclude": "One pattern per line",
    "exclude_files": "One exclude file per line",
    "extra_rclone_args": "One argument per line",
    "extra_restic_backup_args": "One argument per line",
    "extra_restic_forget_args": "One argument per line",
    "extra_restic_prune_args": "One argument per line",
    "keep_within": "e.g. 1y2m3d4h",
    "keep_within_hourly": "e.g. 7d",
    "keep_within_daily": "e.g. 1m",
    "keep_within_weekly": "e.g. 1y",
    "keep_within_monthly": "e.g. 5y",
    "keep_within_yearly": "e.g. 75y",
    "source_files": "One file with source paths per line",
    "sources": "One absolute path per line",
    "tags": "One tag per line",
    "pre_hooks": "One command per line",
    "post_hooks": "One command per line",
    "on_error_hooks": "One command per line",
}

_RESTIC_RETENTION_PANEL: dict[str, Any] = {
    "backend": "restic",
    "title": "Restic retention and cleanup",
    "lines": (
        "Retention runs restic forget with the configured keep rules.",
        "Cleanup runs restic prune and is the step that actually removes unused repository data.",
        "Without keep rules, retention has no useful policy and is rejected by validation.",
    ),
}

_RESTIC_EXTRA_ARGS_PANEL: dict[str, Any] = {
    "backend": "restic",
    "title": "Additional Restic arguments",
    "lines": (
        "Values are passed directly to the matching restic command.",
        "Use one complete argument per line, for example --verbose or --option key=value.",
        "For inherited lists, an explicit empty override stops inheritance.",
    ),
}

_RCLONE_EXTRA_ARGS_PANEL: dict[str, Any] = {
    "backend": "rclone",
    "title": "Additional Rclone arguments",
    "lines": (
        "Values are passed directly to rclone sync.",
        "Use one complete argument per line, for example --fast-list.",
        "For inherited lists, an explicit empty override stops inheritance.",
    ),
}

_HOOKS_PANEL: dict[str, Any] = {
    "title": "Hook execution",
    "help_anchor": "hooks",
    "lines": (
        "Script hooks must use absolute paths inside DK_SCRIPTS_DIR.",
        "Inline shell commands are blocked unless DK_ALLOW_INLINE_HOOKS=true is set.",
        "Pre and on-error hook failures stop the run; post hook failures only warn.",
    ),
}

_SOURCE_FILES_PANEL: dict[str, Any] = {
    "backend": "restic",
    "title": "Source file lists",
    "lines": (
        "These are files containing backup source paths, not files to back up directly.",
        "Each referenced file is passed to restic backup as a source list.",
        "Source paths inside the files should be absolute paths.",
    ),
}

_SOURCES_PANEL: dict[str, Any] = {
    "backend": "restic",
    "title": "Finding source paths",
    "lines": (
        "Source paths must be absolute paths; shell patterns are not expanded here.",
        "In a terminal, change into the parent directory and run: "
        "find \"$(pwd)\" -maxdepth 1 -name 'Project*'",
        "Add -type d to list only directories, or -type f to list only files.",
        "Copy the listed paths into this field, one path per line.",
    ),
}

_SYNC_DELETE_PANEL: dict[str, Any] = {
    "backend": "rclone",
    "title": "Rclone sync deletion",
    "lines": (
        "When enabled, rclone sync may delete target files that no longer exist at the source.",
        (
            "Dockkeep blocks delete-sync runs for empty, missing, or unreadable "
            "local source directories."
        ),
        "Use this only when the target should mirror the source.",
    ),
    "help_anchor": "rclone",
}

_NOTIFICATION_ENV_PANEL: dict[str, Any] = {
    "title": "Notification credentials via environment",
    "lines": (
        "Define the variable in .env; docker-compose.yml loads it via env_file.",
        "Enter only the variable name here, not the secret value.",
        "Commented values in .env remain unavailable inside the container.",
    ),
}

_PROVIDER_EVENTS_PANEL: dict[str, Any] = {
    "title": "Channel routing",
    "help_anchor": "notifications",
    "lines": (
        "Valid entries, one per line: success, error, skipped, report.",
        "Leave empty to route every event to this channel.",
        (
            "This only narrows what the channel carries. Whether an event is raised at "
            "all stays with the notification triggers of the global and job settings."
        ),
    ),
}

_INFO_PANELS: dict[tuple[str, str], tuple[dict[str, Any], ...]] = {
    ("backup", "exclude"): (
        {
            "backend": "restic",
            "title": "Restic exclude patterns",
            "lines": (
                "Patterns are passed to restic backup as --exclude options.",
                "They use Go filepath.Match syntax plus ** and are tested against the full path.",
                (
                    "Patterns match complete path components; a leading / anchors at "
                    "the snapshot root."
                ),
            ),
        },
        {
            "backend": "rclone",
            "title": "Rclone exclude patterns",
            "lines": (
                "Patterns are passed to rclone sync as --exclude options.",
                "* matches within one path segment; ** may match across / separators.",
                "A leading / anchors at the transfer root; otherwise complete path segments match.",
            ),
        },
    ),
    ("backup", "exclude_files"): (
        {
            "backend": "restic",
            "title": "Restic exclude files",
            "lines": (
                "Use one absolute file path per line.",
                (
                    "Each file contains restic exclude patterns; empty lines and # comments "
                    "are ignored."
                ),
                "Environment variables are expanded in exclude files; ~ is not expanded.",
            ),
        },
    ),
    ("backup", "sources"): (_SOURCES_PANEL,),
    ("backup", "source_files"): (_SOURCE_FILES_PANEL,),
    ("backup", "retention"): (_RESTIC_RETENTION_PANEL,),
    ("backup", "cleanup"): (_RESTIC_RETENTION_PANEL,),
    ("backup", "extra_restic_backup_args"): (_RESTIC_EXTRA_ARGS_PANEL,),
    ("backup", "extra_restic_forget_args"): (_RESTIC_EXTRA_ARGS_PANEL,),
    ("backup", "extra_restic_prune_args"): (_RESTIC_EXTRA_ARGS_PANEL,),
    ("backup", "pre_hooks"): (_HOOKS_PANEL,),
    ("backup", "post_hooks"): (_HOOKS_PANEL,),
    ("backup", "on_error_hooks"): (_HOOKS_PANEL,),
    ("job.backup", "exclude"): (
        {
            "backend": "restic",
            "title": "Restic exclude patterns",
            "lines": (
                "These defaults are inherited by restic backup tasks unless overridden.",
                (
                    "Patterns use Go filepath.Match syntax plus ** and are tested against "
                    "the full path."
                ),
                (
                    "Patterns match complete path components; a leading / anchors at "
                    "the snapshot root."
                ),
            ),
        },
    ),
    ("job.backup", "exclude_files"): (
        {
            "backend": "restic",
            "title": "Restic exclude files",
            "lines": (
                "These defaults are inherited by restic backup tasks unless overridden.",
                "Use one absolute file path per line.",
                (
                    "Each file contains restic exclude patterns; empty lines and # comments "
                    "are ignored."
                ),
            ),
        },
    ),
    ("job.backup", "retention"): (_RESTIC_RETENTION_PANEL,),
    ("job.backup", "cleanup"): (_RESTIC_RETENTION_PANEL,),
    ("job.backup", "extra_restic_backup_args"): (_RESTIC_EXTRA_ARGS_PANEL,),
    ("job.backup", "extra_restic_forget_args"): (_RESTIC_EXTRA_ARGS_PANEL,),
    ("job.backup", "extra_restic_prune_args"): (_RESTIC_EXTRA_ARGS_PANEL,),
    ("rclone", "exclude"): (
        {
            "backend": "rclone",
            "title": "Rclone exclude patterns",
            "lines": (
                "Patterns are passed to rclone sync as --exclude options.",
                "* matches within one path segment; ** may match across / separators.",
                "A leading / anchors at the transfer root; otherwise complete path segments match.",
            ),
        },
    ),
    ("rclone", "sync_delete"): (_SYNC_DELETE_PANEL,),
    ("rclone", "extra_rclone_args"): (_RCLONE_EXTRA_ARGS_PANEL,),
    ("rclone", "pre_hooks"): (_HOOKS_PANEL,),
    ("rclone", "post_hooks"): (_HOOKS_PANEL,),
    ("rclone", "on_error_hooks"): (_HOOKS_PANEL,),
    ("rclone", "filter_from"): (
        {
            "backend": "rclone",
            "title": "Rclone filter file",
            "lines": (
                "The file is passed to rclone as --filter-from.",
                "Use + pattern to include and - pattern to exclude; rule order matters.",
                "Blank lines and # comments are ignored; use / as the path separator.",
            ),
        },
    ),
    ("job.rclone", "sync_delete"): (_SYNC_DELETE_PANEL,),
    ("job.rclone", "extra_rclone_args"): (_RCLONE_EXTRA_ARGS_PANEL,),
    ("job.rclone", "exclude"): (
        {
            "backend": "rclone",
            "title": "Rclone exclude patterns",
            "lines": (
                "These defaults are inherited by rclone sync tasks unless overridden.",
                "* matches within one path segment; ** may match across / separators.",
                "A leading / anchors at the transfer root; otherwise complete path segments match.",
            ),
        },
    ),
    ("job.rclone", "filter_from"): (
        {
            "backend": "rclone",
            "title": "Rclone filter file",
            "lines": (
                "These defaults are inherited by rclone sync tasks unless overridden.",
                "Use + pattern to include and - pattern to exclude; rule order matters.",
                "Blank lines and # comments are ignored; use / as the path separator.",
            ),
        },
    ),
    ("job", "pre_hooks"): (_HOOKS_PANEL,),
    ("job", "post_hooks"): (_HOOKS_PANEL,),
    ("job", "on_error_hooks"): (_HOOKS_PANEL,),
    ("workflow", "pre_hooks"): (_HOOKS_PANEL,),
    ("workflow", "post_hooks"): (_HOOKS_PANEL,),
    ("workflow", "on_error_hooks"): (_HOOKS_PANEL,),
}

_PLACEHOLDERS: dict[str, str] = {
    "keep_within": "1y2m3d4h",
    "keep_within_hourly": "7d",
    "keep_within_daily": "1m",
    "keep_within_weekly": "1y",
    "keep_within_monthly": "5y",
    "keep_within_yearly": "75y",
    "report_schedule": "0 8 * * *",
    "target": "myremote:bucket/path",
    "schedule": "0 2 * * *",
}

_DEFAULT_HINTS: dict[tuple[str, str], str] = {
    ("global", "log_level"): "debug",
    ("global", "notify_on_success"): "no",
    ("global", "notify_on_error"): "no",
    ("global", "notify_on_skipped"): "no",
}

# Default hints apply only where [global.backup] is inherited.
_JOB_BACKUP_DEFAULT_HINTS: dict[str, str] = {
    "retention": "no",
    "cleanup": "no",
    "auto_init": "no",
}

_CHOICES: dict[str, tuple[str, ...]] = {
    "backend": ("restic",),
    "log_level": ("debug", "info", "warning", "error", "critical"),
}


def _field_from_domain(
    domain: ConfigField,
    *,
    key: str | None = None,
    label: str | None = None,
    inheritable: bool | None = None,
    optional: bool = True,
    parent_key: str | None = None,
    default_hint: str | None = None,
) -> EditorField:
    editor_kind = domain.editor_kind or "text"
    kind = _EDITOR_KIND_MAP.get(editor_kind, "text")
    return EditorField(
        key=key if key is not None else domain.key,
        label=label if label is not None else _LABELS.get(domain.key, domain.key),
        kind=kind,
        cron=editor_kind == "cron",
        inheritable=domain.inheritance != "none" if inheritable is None else inheritable,
        optional=optional,
        choices=_CHOICES.get(domain.key, ()),
        placeholder=_PLACEHOLDERS.get(domain.key, ""),
        help_text=_HELP_TEXTS.get(domain.key, ""),
        info_panels=_INFO_PANELS.get((domain.level, domain.key), ()),
        parent_key=parent_key if parent_key is not None else domain.parent_key,
        default_hint=(
            default_hint
            if default_hint is not None
            else _DEFAULT_HINTS.get((domain.level, domain.key), "")
        ),
        domain_field=domain,
    )


def _domain_field(level: str, key: str, *, parent_level: str | None = None) -> ConfigField:
    for item in fields_for_level(level):  # type: ignore[arg-type]
        if item.key == key and (parent_level is None or item.parent_level == parent_level):
            return item
    raise KeyError(f"No domain field for {level}.{key}")


def _fields_from_domain(
    level: str,
    keys: tuple[str, ...],
    *,
    parent_level: str | None = None,
    overrides: Mapping[str, dict[str, Any]] | None = None,
) -> tuple[EditorField, ...]:
    overrides = overrides or {}
    return tuple(
        _field_from_domain(
            _domain_field(level, key, parent_level=parent_level),
            **overrides.get(key, {}),
        )
        for key in keys
    )


_BACKUP_DEFAULT_KEYS: tuple[str, ...] = (
    "retention",
    "cleanup",
    "auto_init",
    "exclude",
    "exclude_files",
    "exclude_caches",
    "one_file_system",
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
    "extra_restic_backup_args",
    "extra_restic_forget_args",
    "extra_restic_prune_args",
    "backup_timeout",
)

_RCLONE_DEFAULT_KEYS: tuple[str, ...] = (
    "transfers",
    "checkers",
    "bwlimit",
    "sync_delete",
    "exclude",
    "filter_from",
    "extra_rclone_args",
    "rclone_timeout",
)

GLOBAL_BACKUP_FIELDS = _fields_from_domain(
    "job.backup",
    _BACKUP_DEFAULT_KEYS,
    parent_level="global.backup",
    overrides={key: {"inheritable": False} for key in _BACKUP_DEFAULT_KEYS},
)

GLOBAL_RCLONE_FIELDS = _fields_from_domain(
    "job.rclone",
    _RCLONE_DEFAULT_KEYS,
    parent_level="global.rclone",
    overrides={key: {"inheritable": False} for key in _RCLONE_DEFAULT_KEYS},
)

JOB_BACKUP_DEFAULTS_FIELDS = _fields_from_domain(
    "job.backup",
    _BACKUP_DEFAULT_KEYS,
    parent_level="global.backup",
    overrides={key: {"default_hint": hint} for key, hint in _JOB_BACKUP_DEFAULT_HINTS.items()},
)

JOB_RCLONE_DEFAULTS_FIELDS = _fields_from_domain(
    "job.rclone",
    _RCLONE_DEFAULT_KEYS,
    parent_level="global.rclone",
)

JOB_FIELDS = _fields_from_domain(
    "job",
    (
        "hook_timeout",
        "notify_on_success",
        "notify_on_error",
        "notify_on_skipped",
        "pre_hooks",
        "post_hooks",
        "on_error_hooks",
    ),
)

BACKUP_FIELDS = _fields_from_domain(
    "backup",
    (
        "sources",
        "source_files",
        "schedule",
        "backend",
        "retention",
        "cleanup",
        "auto_init",
        "exclude",
        "exclude_files",
        "exclude_caches",
        "one_file_system",
        "tags",
        "extra_restic_backup_args",
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
        "extra_restic_prune_args",
        "backup_timeout",
        "hook_timeout",
        "notify_on_success",
        "notify_on_error",
        "notify_on_skipped",
        "pre_hooks",
        "post_hooks",
        "on_error_hooks",
    ),
    overrides={
        "sources": {"inheritable": False},
        "source_files": {"inheritable": False},
        "backend": {"inheritable": False},
    },
)

WORKFLOW_FIELDS = _fields_from_domain(
    "workflow",
    (
        "schedule",
        "hook_timeout",
        "notify_on_success",
        "notify_on_error",
        "notify_on_skipped",
        "pre_hooks",
        "post_hooks",
        "on_error_hooks",
    ),
)

RCLONE_FIELDS = _fields_from_domain(
    "rclone",
    (
        "source",
        "target",
        "schedule",
        "sync_delete",
        "transfers",
        "checkers",
        "bwlimit",
        "exclude",
        "filter_from",
        "extra_rclone_args",
        "rclone_timeout",
        "hook_timeout",
        "notify_on_success",
        "notify_on_error",
        "notify_on_skipped",
        "pre_hooks",
        "post_hooks",
        "on_error_hooks",
    ),
    overrides={"source": {"optional": False}, "target": {"optional": False}},
)

GLOBAL_FIELDS = (
    *_fields_from_domain("global", ("log_level", "log_retention_days")),
    field("lock_retry_count", "number", label=_LABELS["lock_retry_count"]),
    field("lock_retry_delay", "number", label=_LABELS["lock_retry_delay"]),
    *_fields_from_domain(
        "job",
        ("hook_timeout",),
        parent_level="global",
        overrides={"hook_timeout": {"inheritable": False}},
    ),
    *_fields_from_domain(
        "global",
        (
            "notify_on_success",
            "notify_on_error",
            "notify_on_skipped",
        ),
    ),
)

GLOBAL_NOTIFICATION_FIELDS = (
    field(
        "report_schedule",
        label=_LABELS["report_schedule"],
        cron=True,
        placeholder=_PLACEHOLDERS["report_schedule"],
    ),
)

MAIL_FIELDS = (
    field("host", label="SMTP server", optional=False),
    field("port", "number", label="Port", default_hint="587"),
    field(
        "connection_security",
        "select",
        label="Connection security",
        choices=("none", "starttls", "ssl"),
        default_hint="starttls",
    ),
    field(
        "username_env",
        label="Username (env var)",
        placeholder="SMTP_USER",
        info_panels=(_NOTIFICATION_ENV_PANEL,),
        default_value="SMTP_USER",
    ),
    field(
        "password_env",
        label="Password (env var)",
        placeholder="SMTP_PASSWORD",
        info_panels=(_NOTIFICATION_ENV_PANEL,),
        default_value="SMTP_PASSWORD",
    ),
    field("from_addr", label="Sender", optional=False),
    field("to", "list", label="Recipients", optional=False, help_text="One address per line"),
    field(
        "events",
        "checklist",
        label="Events",
        choices=("success", "error", "skipped", "report"),
        help_text="No boxes checked = all events",
        info_panels=(_PROVIDER_EVENTS_PANEL,),
    ),
)

PUSHOVER_FIELDS = (
    field(
        "token_env",
        label="API token (env var)",
        optional=False,
        placeholder="PUSHOVER_TOKEN",
        info_panels=(_NOTIFICATION_ENV_PANEL,),
        default_value="PUSHOVER_TOKEN",
    ),
    field(
        "user_key_env",
        label="User key (env var)",
        optional=False,
        placeholder="PUSHOVER_USER_KEY",
        info_panels=(_NOTIFICATION_ENV_PANEL,),
        default_value="PUSHOVER_USER_KEY",
    ),
    field("priority", "number", label="Priority", default_hint="0"),
    field("sound", label="Sound"),
    field("device", label="Device"),
    field(
        "events",
        "checklist",
        label="Events",
        choices=("success", "error", "skipped", "report"),
        help_text="No boxes checked = all events",
        info_panels=(_PROVIDER_EVENTS_PANEL,),
    ),
)


def effective_values(
    table: Mapping[str, Any],
    fields: tuple[EditorField, ...],
    parent: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    values = dict(parent or {})
    for item in fields:
        if item.key in table:
            values[item.key] = table[item.key]
    return values


def field_views(
    table: Mapping[str, Any],
    fields: tuple[EditorField, ...],
    *,
    parent: Mapping[str, Any] | None = None,
    prefix: str = "",
) -> list[dict[str, Any]]:
    inherited = parent or {}
    result: list[dict[str, Any]] = []
    for item in fields:
        explicit = item.key in table
        raw_value = table.get(item.key)
        parent_value = inherited.get(item.parent_key or item.key)

        if item.kind == "bool":
            value = ("true" if bool(raw_value) else "false") if explicit else ""
        else:
            value = _display_value(raw_value if explicit else "", item.kind)

        is_inherited = item.inheritable and not explicit and parent_value is not None
        hint = _display_value(parent_value, item.kind) if is_inherited else item.placeholder

        result.append(
            {
                "name": f"{prefix}{item.key}",
                "mode_name": f"{prefix}{item.key}__mode",
                "empty_name": f"{prefix}{item.key}__empty",
                "label": item.label or item.key,
                "kind": item.kind,
                "cron": item.cron,
                "value": value,
                "selected_choices": _selected_choices(raw_value if explicit else [], item),
                "mode": _list_mode(item, explicit, raw_value),
                "empty_checked": item.kind == "list" and item.inheritable and raw_value == [],
                "hint": hint,
                "default_hint": item.default_hint,
                "choices": item.choices,
                "help_text": item.help_text,
                "info_panels": item.info_panels,
                "inherited": is_inherited,
                "inheritable": item.inheritable,
                "required": not item.optional,
            }
        )
    return result


def apply_fields(
    table: TomlMapping,
    fields: tuple[EditorField, ...],
    form: Mapping[str, str],
    *,
    prefix: str = "",
) -> None:
    for item in fields:
        name = f"{prefix}{item.key}"
        if name not in form:
            continue
        raw = form.get(name, "").strip()
        if item.kind == "bool":
            if raw in ("true", "false"):
                table[item.key] = raw == "true"
            else:
                table.pop(item.key, None)
        elif item.kind == "checklist":
            checked = _checked_choices(form, item, name)
            if checked:
                table[item.key] = checked
            else:
                table.pop(item.key, None)
        elif item.kind == "list" and item.inheritable:
            lines = [line.strip() for line in raw.splitlines() if line.strip()]
            empty_checked = _list_empty_checked(form, name)
            if lines:
                table[item.key] = lines
            elif empty_checked:
                table[item.key] = []
            else:
                table.pop(item.key, None)
        elif not raw:
            table.pop(item.key, None)
        elif item.kind == "list":
            table[item.key] = [line.strip() for line in raw.splitlines() if line.strip()]
        elif item.kind == "number":
            try:
                table[item.key] = int(raw)
            except ValueError:
                raise ValueError(f"Field '{item.key}': {raw!r} is not a valid number")
        else:
            table[item.key] = raw


def pseudo_table_from_form(
    fields: tuple[EditorField, ...],
    form: Mapping[str, str],
    prefix: str = "",
) -> dict[str, Any]:
    """Convert submitted form values to a pseudo-table suitable for field_views.

    Mirrors the parsing logic of apply_fields but returns a plain dict instead of
    writing to a tomlkit table. Invalid number inputs are kept as strings so the
    submitted value is visible after a failed save attempt.
    """
    table: dict[str, Any] = {}
    for item in fields:
        name = f"{prefix}{item.key}"
        if name not in form:
            continue
        raw = form.get(name, "").strip()
        if item.kind == "bool":
            if raw in ("true", "false"):
                table[item.key] = raw == "true"
        elif item.kind == "checklist":
            checked = _checked_choices(form, item, name)
            if checked:
                table[item.key] = checked
        elif item.kind == "list" and item.inheritable:
            lines = [line.strip() for line in raw.splitlines() if line.strip()]
            empty_checked = _list_empty_checked(form, name)
            if lines:
                table[item.key] = lines
            elif empty_checked:
                table[item.key] = []
        elif raw:
            if item.kind == "list":
                table[item.key] = [line.strip() for line in raw.splitlines() if line.strip()]
            elif item.kind == "number":
                try:
                    table[item.key] = int(raw)
                except ValueError:
                    table[item.key] = raw
            else:
                table[item.key] = raw
    return table


def _display_value(value: Any, kind: FieldKind) -> str:
    if value is None:
        return ""
    if kind in {"checklist", "list"}:
        return "\n".join(str(item) for item in value)
    if kind == "bool":
        return str(value).lower()
    return str(value)


def _list_mode(item: EditorField, explicit: bool, value: Any) -> str:
    if item.kind != "list" or not item.inheritable:
        return ""
    if not explicit:
        return "inherit"
    if value == []:
        return "empty"
    return "values"


def _list_empty_checked(form: Mapping[str, str], name: str) -> bool:
    value = form.get(f"{name}__empty")
    return value is not None and value.lower() in {"1", "true", "yes", "on"}


def _checked_choices(form: Mapping[str, str], item: EditorField, name: str) -> list[str]:
    return [
        choice
        for choice in item.choices
        if form.get(f"{name}__{choice}", "").lower() in {"1", "true", "yes", "on"}
    ]


def _selected_choices(value: Any, item: EditorField) -> list[str]:
    if item.kind != "checklist" or not isinstance(value, list):
        return []
    return [str(choice) for choice in value if str(choice) in item.choices]
