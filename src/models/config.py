"""Pydantic Models für die Dockkeep-Konfiguration."""

import re
import shlex
from datetime import datetime
from typing import Literal

from croniter import CroniterBadDateError, croniter
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictConfigModel(BaseModel):
    """Basis für Config-Modelle: unbekannte TOML-Keys sind Fehler."""

    model_config = ConfigDict(extra="forbid")


_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_KEEP_WITHIN_RE = re.compile(r"(?:\d+[ymdh])+")
_KEEP_WITHIN_FIELD_NAMES = (
    "keep_within",
    "keep_within_hourly",
    "keep_within_daily",
    "keep_within_weekly",
    "keep_within_monthly",
    "keep_within_yearly",
)
_RCLONE_BWLIMIT_RATE_RE = r"(?:off|\d+(?:\.\d+)?[kKMGTPE]?)"
_RCLONE_BWLIMIT_VALUE_RE = rf"{_RCLONE_BWLIMIT_RATE_RE}(?::{_RCLONE_BWLIMIT_RATE_RE})?"
_BWLIMIT_RE = re.compile(
    rf"(?:{_RCLONE_BWLIMIT_VALUE_RE}|"
    rf"(?:[01]\d|2[0-3]):[0-5]\d,{_RCLONE_BWLIMIT_VALUE_RE}"
    rf"(?:\s+(?:[01]\d|2[0-3]):[0-5]\d,{_RCLONE_BWLIMIT_VALUE_RE})*)"
)
_RESERVED_JOB_NAMES = {"_system", "__dockkeep_adhoc_restore__"}


def _validate_extra_args(v: list[str] | None) -> list[str] | None:
    """Reject extra args that cannot be split into valid argv tokens."""
    if v is None:
        return v
    for arg in v:
        try:
            tokens = shlex.split(arg)
        except ValueError as exc:
            raise ValueError(
                f"Invalid extra args entry {arg!r}: {exc}. "
                "Check for unmatched quotes or unescaped backslashes."
            ) from exc
        if not tokens:
            raise ValueError(f"Extra args entry must not be empty: {arg!r}")
    return v


def _validate_hook_commands(v: list[str]) -> list[str]:
    """Reject hook entries that cannot produce an executable command."""
    for hook in v:
        try:
            tokens = shlex.split(hook)
        except ValueError as exc:
            raise ValueError(
                f"Invalid hook command {hook!r}: {exc}. "
                "Check for unmatched quotes or unescaped backslashes."
            ) from exc
        if not tokens:
            raise ValueError(f"Hook command must not be empty: {hook!r}")
    return v


def _coerce_empty_string_to_none(v: object) -> object:
    if v == "":
        return None
    return v


def _validate_env_var_name(v: str | None) -> str | None:
    """Reject empty or whitespace-only environment variable names."""
    if v is not None and v.strip() == "":
        raise ValueError("environment variable name must not be empty")
    return v


def _validate_bwlimit(v: str | None) -> str | None:
    if v is None:
        return v
    if not _BWLIMIT_RE.fullmatch(v):
        raise ValueError(
            f"Invalid bwlimit format: {v!r}. "
            "Expected format like '10M', '1.5M', 'off', '100k:1M', "
            "or timetable entries like '08:00,512k 12:00,off'."
        )
    return v


def _validate_keep_within(v: str | None) -> str | None:
    if v is None or v == "":
        return None
    if not _KEEP_WITHIN_RE.fullmatch(v):
        raise ValueError(
            f"Invalid keep_within format: {v!r}. "
            "Expected one or more restic duration units y, m, d, h, "
            "e.g. '7d', '2m3d', '1y2m3d4h'."
        )
    return v


def _validate_config_names(kind: str, names: object, *, reserved: set[str] | None = None) -> None:
    """Validate dynamic TOML table names."""
    if not isinstance(names, dict):
        return
    reserved = reserved or set()
    for key in names:
        if not _NAME_RE.fullmatch(key):
            raise ValueError(
                f"Invalid {kind} name: {key!r}. "
                "Names must match ^[A-Za-z0-9_-]+$ (letters, digits, hyphens, underscores)."
            )
        if key in reserved:
            raise ValueError(
                f"Invalid {kind} name: {key!r}. "
                f"{key!r} is reserved and cannot be used as a {kind} name."
            )


def _normalize_dynamic_task_section(
    data: object, *, default_keys: set[str], section_name: str
) -> object:
    """Move dynamic TOML subtables into the explicit ``tasks`` field."""
    if not isinstance(data, dict):
        return data

    normalized: dict[str, object] = {}
    tasks: dict[str, object] = {}
    if "tasks" in data:
        raise ValueError(
            f"{section_name}.tasks is internal and cannot be provided in TOML/input. "
            f"Define task tables directly under {section_name}."
        )

    for key, value in data.items():
        if key == "tasks":
            continue
        if key in default_keys:
            normalized[key] = value
            continue
        if isinstance(value, (dict, BaseModel)):
            tasks[key] = value
            continue
        raise ValueError(
            f"Unknown field in {section_name}: {key!r}. "
            "Dynamic task entries must be TOML tables."
        )

    normalized["tasks"] = tasks
    return normalized


def _normalize_schedule(v: object) -> object:
    """Normalisiert leere/whitespace-only Schedule-Strings zu None.

    `None` bleibt `None` (kein Schedule, nicht gesetzt). Strings werden
    getrimmt; ist das Ergebnis leer, wird `None` zurückgegeben (= kein
    Schedule). Andernfalls wird der ursprüngliche Wert unverändert für die
    nachgelagerte Cron-Validierung zurückgegeben.

    Args:
        v: Der rohe Schedule-Wert vor der Cron-Validierung.

    Returns:
        `None`, falls `v` `None` oder ein leerer/whitespace-only String ist;
        andernfalls den unveränderten Wert `v`.
    """
    if v is None:
        return None
    if isinstance(v, str) and v.strip() == "":
        return None
    return v


def _validate_cron_schedule(v: object, *, example: str) -> object:
    """Validate five-field cron expressions, including impossible schedules."""
    v = _normalize_schedule(v)
    if v is None:
        return None

    value = str(v)
    if len(value.split()) != 5 or not croniter.is_valid(value):
        raise ValueError(
            f"Invalid cron expression: {v!r}. "
            "Expected 5-field cron format (minute hour day month weekday), "
            f"e.g. {example}."
        )
    try:
        croniter(value, datetime.now().astimezone()).get_next(datetime)
    except CroniterBadDateError as exc:
        raise ValueError(
            f"Invalid cron expression: {v!r}. "
            "The expression is valid syntax but has no reachable next run."
        ) from exc
    return v


class RawGlobalBackupConfig(StrictConfigModel):
    """Globale Backup-Defaults im Ziel-TOML (``[global.backup]``)."""

    password: str | None = Field(None, min_length=1)
    password_env: str | None = None
    password_file: str | None = None
    backup_timeout: int | None = Field(None, ge=1)
    retention: bool = False
    cleanup: bool = False
    auto_init: bool = False
    keep_last: int | None = Field(None, ge=1)
    keep_hourly: int | None = Field(None, ge=1)
    keep_daily: int | None = Field(None, ge=1)
    keep_weekly: int | None = Field(None, ge=1)
    keep_monthly: int | None = Field(None, ge=1)
    keep_yearly: int | None = Field(None, ge=1)
    keep_within: str | None = None
    keep_within_hourly: str | None = None
    keep_within_daily: str | None = None
    keep_within_weekly: str | None = None
    keep_within_monthly: str | None = None
    keep_within_yearly: str | None = None
    exclude: list[str] | None = None
    exclude_files: list[str] | None = None
    exclude_caches: bool | None = None
    one_file_system: bool | None = None
    extra_restic_backup_args: list[str] | None = None
    extra_restic_forget_args: list[str] | None = None
    extra_restic_prune_args: list[str] | None = None

    @field_validator("password_env")
    @classmethod
    def validate_env_var_name(cls, v: str | None) -> str | None:
        return _validate_env_var_name(v)

    @model_validator(mode="after")
    def validate_password_exclusive(self) -> "RawGlobalBackupConfig":
        if self.password is not None and self.password_env is not None:
            raise ValueError("password and password_env are mutually exclusive")
        if self.password is not None and self.password_file is not None:
            raise ValueError("password and password_file are mutually exclusive")
        if self.password_env is not None and self.password_file is not None:
            raise ValueError("password_env and password_file are mutually exclusive")
        return self

    @field_validator(*_KEEP_WITHIN_FIELD_NAMES)
    @classmethod
    def validate_keep_within(cls, v: str | None) -> str | None:
        return _validate_keep_within(v)

    @field_validator(
        "extra_restic_backup_args",
        "extra_restic_forget_args",
        "extra_restic_prune_args",
    )
    @classmethod
    def validate_extra_args(cls, v: list[str] | None) -> list[str] | None:
        return _validate_extra_args(v)


class RawGlobalRcloneConfig(StrictConfigModel):
    """Globale Rclone-Defaults, die auf Job-/Task-Ebene überschrieben werden können.

    extra_rclone_args bleibt None, wenn es nicht gesetzt wurde, damit eine explizit
    leere Liste die Vererbung stoppen kann.
    """

    transfers: int | None = Field(None, ge=1)
    checkers: int | None = Field(None, ge=1)
    bwlimit: str | None = None
    rclone_timeout: int | None = Field(None, ge=1)
    sync_delete: bool | None = None
    exclude: list[str] | None = None
    filter_from: str | None = None
    extra_rclone_args: list[str] | None = None

    @field_validator("extra_rclone_args")
    @classmethod
    def validate_extra_args(cls, v: list[str] | None) -> list[str] | None:
        return _validate_extra_args(v)

    @field_validator("bwlimit", mode="before")
    @classmethod
    def coerce_empty_bwlimit(cls, v: object) -> object:
        return _coerce_empty_string_to_none(v)

    @field_validator("bwlimit")
    @classmethod
    def validate_bwlimit(cls, v: str | None) -> str | None:
        return _validate_bwlimit(v)


def _default_raw_global_backup_config() -> RawGlobalBackupConfig:
    return RawGlobalBackupConfig.model_validate({})


def _default_raw_global_rclone_config() -> RawGlobalRcloneConfig:
    return RawGlobalRcloneConfig.model_validate({})


NotificationEventKind = Literal["success", "error", "skipped", "report"]
"""Ereignisart, die ein Notification-Provider transportieren kann.

``success``/``error``/``skipped`` entsprechen den terminalen Run-Ausgängen der
Task-Policy, ``report`` dem periodischen Run-Bericht.
"""


class RawMailNotificationConfig(StrictConfigModel):
    """SMTP provider config ([global.notifications.mail]).

    Attributes:
        host: SMTP server hostname.
        port: SMTP server port (1-65535).
        connection_security: TLS mode – "starttls" (port 587), "ssl" (port 465), or "none".
        username_env: Env var name for the SMTP username (optional).
        password_env: Env var name for the SMTP password (optional).
        from_addr: Sender address.
        to: Recipient addresses (at least one).
        events: Ereignisarten, die über diesen Kanal gehen. None = alle.
    """

    host: str
    port: int = 587
    connection_security: Literal["none", "starttls", "ssl"] = "starttls"
    username_env: str | None = None
    password_env: str | None = None
    from_addr: str
    to: list[str] = Field(min_length=1)
    events: list[NotificationEventKind] | None = Field(None, min_length=1)

    @field_validator("port")
    @classmethod
    def validate_port(cls, v: int) -> int:
        if not 1 <= v <= 65535:
            raise ValueError("port must be between 1 and 65535")
        return v

    @field_validator("username_env", "password_env")
    @classmethod
    def validate_env_var_name(cls, v: str | None) -> str | None:
        return _validate_env_var_name(v)

    @model_validator(mode="after")
    def validate_consistency(self) -> "RawMailNotificationConfig":
        if (self.username_env is None) != (self.password_env is None):
            raise ValueError("username_env and password_env must be configured together")
        return self


class RawPushoverNotificationConfig(StrictConfigModel):
    """Pushover provider config ([global.notifications.pushover]).

    Attributes:
        token_env: Env var name for the Pushover API token.
        user_key_env: Env var name for the Pushover user key.
        priority: Message priority (-2 to 1; priority 2 is not supported).
        sound: Pushover sound name; empty string treated as None.
        device: Target device name; empty string treated as None.
        events: Ereignisarten, die über diesen Kanal gehen. None = alle.
    """

    token_env: str
    user_key_env: str
    priority: int = 0
    sound: str | None = None
    device: str | None = None
    events: list[NotificationEventKind] | None = Field(None, min_length=1)

    @field_validator("priority")
    @classmethod
    def validate_priority(cls, v: int) -> int:
        if not -2 <= v <= 1:
            raise ValueError(
                f"priority must be between -2 and 1, got {v}. "
                "Priority 2 (emergency) is not supported because it requires "
                "additional 'expire' and 'retry' parameters."
            )
        return v

    @field_validator("token_env", "user_key_env")
    @classmethod
    def validate_env_var_name(cls, v: str) -> str:
        validated = _validate_env_var_name(v)
        if validated is None:
            raise ValueError("environment variable name must not be empty")
        return validated

    @field_validator("sound", "device", mode="before")
    @classmethod
    def coerce_empty_string(cls, v: object) -> object:
        if v == "":
            return None
        return v


class RawGlobalNotificationsConfig(StrictConfigModel):
    """Global notification settings ([global.notifications]).

    Attributes:
        report_schedule: Cron schedule for periodic run summary reports.
        mail: SMTP provider config (active when present and valid).
        pushover: Pushover provider config (active when present and valid).
    """

    report_schedule: str | None = None
    mail: RawMailNotificationConfig | None = None
    pushover: RawPushoverNotificationConfig | None = None

    @field_validator("report_schedule", mode="before")
    @classmethod
    def validate_report_schedule(cls, v: object) -> object:
        """Validate the optional periodic report cron expression."""
        return _validate_cron_schedule(v, example="'0 2 * * *'")


class RawGlobalConfig(StrictConfigModel):
    """Globale Konfiguration ([global]-Sektion im TOML).

    Attributes:
        log_level: Log-Level für alle Jobs.
        log_retention_days: Anzahl Tage, für die Logs aufbewahrt werden.
        backup: Globale Backup-Defaults.
        rclone: Globale Rclone-Defaults.
        notifications: Globale Notification-Einstellungen.
        hook_timeout: Hook-Timeout, vererbt global → job → backup. None = kein Timeout.
    """

    log_level: Literal["debug", "info", "warning", "error", "critical"] = "debug"
    log_retention_days: int | None = Field(None, ge=1)
    lock_retry_count: int | None = Field(None, ge=0)
    lock_retry_delay: int | None = Field(None, ge=1)
    backup: RawGlobalBackupConfig = Field(default_factory=_default_raw_global_backup_config)
    rclone: RawGlobalRcloneConfig = Field(default_factory=_default_raw_global_rclone_config)
    notifications: RawGlobalNotificationsConfig = Field(
        default_factory=RawGlobalNotificationsConfig
    )
    hook_timeout: int | None = Field(None, ge=1)
    notify_on_success: bool = False
    notify_on_error: bool = False
    notify_on_skipped: bool = False


def _default_raw_global_config() -> RawGlobalConfig:
    return RawGlobalConfig.model_validate({})


class RawRcloneSyncTaskConfig(StrictConfigModel):
    """Konfiguration für den Rclone-Sync-Step ([jobs.<name>.rclone.<task>]).

    Attributes:
        source: Quellpfad (lokal), typischerweise das lokale Repository.
        target: Ziel-Pfad auf dem Rclone-Remote (z.B. "remote:bucket/repo").
        sync_delete: Dateien im Ziel löschen, die in der Quelle fehlen.
        transfers: Anzahl paralleler Transfers (überschreibt global.rclone).
        checkers: Anzahl paralleler Checker (überschreibt global.rclone).
        bwlimit: Bandbreitenlimit (überschreibt global.rclone, z.B. "10M").
        exclude: Liste von Exclude-Patterns.
        filter_from: Pfad zu einer Rclone-Filter-Datei.
        extra_rclone_args: Zusätzliche Argumente, die ans rclone-Kommando angehängt werden;
            None bedeutet nicht gesetzt, [] bedeutet explizit leer.
        rclone_timeout: Timeout für rclone-Subprozesse auf Task-Ebene. None = Job/Global übernehmen.
        hook_timeout: Timeout für Hook-Subprozesse auf Task-Ebene. None = Job/Global übernehmen.
        schedule: Cron-Ausdruck für automatische Ausführung (leer/None = nur manuell).
        notify_on_*: Benachrichtigungs-Trigger (überschreibt Job-Ebene).
    """

    source: str
    target: str
    sync_delete: bool | None = None
    transfers: int | None = Field(None, ge=1)
    checkers: int | None = Field(None, ge=1)
    bwlimit: str | None = None
    exclude: list[str] | None = None
    filter_from: str | None = None
    extra_rclone_args: list[str] | None = None
    rclone_timeout: int | None = Field(None, ge=1)
    hook_timeout: int | None = Field(None, ge=1)
    schedule: str | None = None
    notify_on_success: bool | None = None
    notify_on_error: bool | None = None
    notify_on_skipped: bool | None = None
    pre_hooks: list[str] = Field(default_factory=list)
    post_hooks: list[str] = Field(default_factory=list)
    on_error_hooks: list[str] = Field(default_factory=list)

    @field_validator("schedule", mode="before")
    @classmethod
    def validate_schedule(cls, v: object) -> object:
        """Validates the cron expression.

        Empty or whitespace-only input is normalized to None (= no schedule).
        """
        return _validate_cron_schedule(
            v,
            example="'0 3 * * *' (daily at 03:00) or '0 2 * * 1' (Mondays at 02:00)",
        )

    @field_validator("extra_rclone_args")
    @classmethod
    def validate_extra_args(cls, v: list[str] | None) -> list[str] | None:
        return _validate_extra_args(v)

    @field_validator("pre_hooks", "post_hooks", "on_error_hooks")
    @classmethod
    def validate_hook_commands(cls, v: list[str]) -> list[str]:
        return _validate_hook_commands(v)

    @field_validator("bwlimit", mode="before")
    @classmethod
    def coerce_empty_bwlimit(cls, v: object) -> object:
        return _coerce_empty_string_to_none(v)

    @field_validator("bwlimit")
    @classmethod
    def validate_bwlimit(cls, v: str | None) -> str | None:
        return _validate_bwlimit(v)


_STEP_RE = re.compile(
    r"^(backup\.[a-zA-Z0-9_-]+(\.backup|\.retention|\.cleanup)?|rclone\.[a-zA-Z0-9_-]+)$"
)


class RawBackupConfig(StrictConfigModel):
    """Konfiguration für ein Backup ([jobs.<name>.backup.<backup-name>]).

    Ein Backup repräsentiert ein einzelnes Restic-Repository-Ziel mit allen
    zugehörigen Backup-, Forget- und Prune-Optionen.

    Attributes:
        repository: Direkter Repository-Pfad (lokaler Pfad oder rclone:remote:path).
        schedule: Cron-Ausdruck für automatische Ausführung (leer/None = nur manuell).
        sources: Quellpfade für dieses Backup.
        password: Direktes Passwort (nicht empfohlen).
        password_env: Umgebungsvariable für das Passwort.
        password_file: Absoluter Pfad zu einer Datei, die das Passwort enthält.
        retention: Retention nach backup ausführen.
        cleanup: Cleanup nach retention ausführen.
        auto_init: Repository anlegen wenn nicht vorhanden.
        exclude: Exclude-Patterns (--exclude).
        source_files: Pfade zu Dateien mit Quellpfaden (--files-from).
        exclude_files: Pfade zu Exclude-Dateien (--exclude-file).
        exclude_caches: None = Wert aus Job/Global übernehmen.
        one_file_system: None = Wert aus Job/Global übernehmen.
        tags: Tags, die dem Snapshot angehängt werden.
        keep_last: Anzahl der neuesten Snapshots, die behalten werden.
        keep_hourly: Anzahl stündlicher Snapshots.
        keep_daily: Anzahl täglicher Snapshots.
        keep_weekly: Anzahl wöchentlicher Snapshots.
        keep_monthly: Anzahl monatlicher Snapshots.
        keep_yearly: Anzahl jährlicher Snapshots.
        keep_within: Snapshots innerhalb dieser Zeitspanne behalten (z.B. "2m3d").
        keep_within_hourly: Stündliche Snapshots innerhalb dieser Zeitspanne behalten.
        keep_within_daily: Tägliche Snapshots innerhalb dieser Zeitspanne behalten.
        keep_within_weekly: Wöchentliche Snapshots innerhalb dieser Zeitspanne behalten.
        keep_within_monthly: Monatliche Snapshots innerhalb dieser Zeitspanne behalten.
        keep_within_yearly: Jährliche Snapshots innerhalb dieser Zeitspanne behalten.
        backup_timeout: Timeout für Backup-Subprozesse auf Backup-Ebene. None = erben.
        hook_timeout: Timeout für Hook-Subprozesse auf Backup-Ebene. None = erben.
    """

    repository: str
    schedule: str | None = None
    backend: Literal["restic"] = "restic"
    sources: list[str] | None = None
    source_files: list[str] | None = None
    password: str | None = Field(None, min_length=1)
    password_env: str | None = None
    password_file: str | None = None
    retention: bool | None = None
    cleanup: bool | None = None
    auto_init: bool | None = None
    exclude: list[str] | None = None
    exclude_files: list[str] | None = None
    exclude_caches: bool | None = None
    one_file_system: bool | None = None
    tags: list[str] = Field(default_factory=list)
    keep_last: int | None = Field(None, ge=1)
    keep_hourly: int | None = Field(None, ge=1)
    keep_daily: int | None = Field(None, ge=1)
    keep_weekly: int | None = Field(None, ge=1)
    keep_monthly: int | None = Field(None, ge=1)
    keep_yearly: int | None = Field(None, ge=1)
    keep_within: str | None = None
    keep_within_hourly: str | None = None
    keep_within_daily: str | None = None
    keep_within_weekly: str | None = None
    keep_within_monthly: str | None = None
    keep_within_yearly: str | None = None
    backup_timeout: int | None = Field(None, ge=1)
    hook_timeout: int | None = Field(None, ge=1)
    extra_restic_backup_args: list[str] | None = None
    extra_restic_forget_args: list[str] | None = None
    extra_restic_prune_args: list[str] | None = None
    notify_on_success: bool | None = None
    notify_on_error: bool | None = None
    notify_on_skipped: bool | None = None
    pre_hooks: list[str] = Field(default_factory=list)
    post_hooks: list[str] = Field(default_factory=list)
    on_error_hooks: list[str] = Field(default_factory=list)

    @field_validator("password_env")
    @classmethod
    def validate_env_var_name(cls, v: str | None) -> str | None:
        return _validate_env_var_name(v)

    @field_validator("pre_hooks", "post_hooks", "on_error_hooks")
    @classmethod
    def validate_hook_commands(cls, v: list[str]) -> list[str]:
        return _validate_hook_commands(v)

    @field_validator(
        "extra_restic_backup_args",
        "extra_restic_forget_args",
        "extra_restic_prune_args",
    )
    @classmethod
    def validate_extra_args(cls, v: list[str] | None) -> list[str] | None:
        return _validate_extra_args(v)

    @model_validator(mode="after")
    def validate_password_exclusive(self) -> "RawBackupConfig":
        """Stellt sicher, dass Passwortfelder sich gegenseitig ausschließen.

        Returns:
            Die validierte RawBackupConfig-Instanz.

        Raises:
            ValueError: Wenn mehr als eines der Passwort-Felder gesetzt ist.
        """
        if self.password is not None and self.password_env is not None:
            raise ValueError("password and password_env are mutually exclusive")
        if self.password is not None and self.password_file is not None:
            raise ValueError("password and password_file are mutually exclusive")
        if self.password_env is not None and self.password_file is not None:
            raise ValueError("password_env and password_file are mutually exclusive")
        return self

    @field_validator(*_KEEP_WITHIN_FIELD_NAMES)
    @classmethod
    def validate_keep_within(cls, v: str | None) -> str | None:
        return _validate_keep_within(v)

    @field_validator("schedule", mode="before")
    @classmethod
    def validate_schedule(cls, v: object) -> object:
        """Validiert den Cron-Ausdruck.

        Leere oder whitespace-only Eingaben werden zu None normalisiert
        (= kein Schedule).

        Args:
            v: Der zu prüfende Cron-Ausdruck.

        Returns:
            `None`, falls `v` `None` oder ein leerer/whitespace-only String ist;
            andernfalls den unveränderten Wert bei Gültigkeit.

        Raises:
            ValueError: Wenn der Ausdruck kein gültiges Cron-Format hat.
        """
        return _validate_cron_schedule(
            v,
            example="'0 3 * * *' (daily at 03:00) or '0 2 * * 1' (Mondays at 02:00)",
        )


class RawWorkflowConfig(StrictConfigModel):
    """Konfiguration für einen Workflow ([jobs.<job>.workflow.<name>]).

    Der Workflow-Name ergibt sich aus dem Dict-Key, nicht aus einem eigenen Feld.

    Attributes:
        schedule: Cron-Ausdruck für automatische Ausführung (leer/None = nur manuell).
        steps: Geordnete Liste von Steps, die sequenziell ausgeführt werden.
    """

    schedule: str | None = None
    steps: list[str] = Field(min_length=1)
    hook_timeout: int | None = Field(None, ge=1)
    notify_on_success: bool | None = None
    notify_on_error: bool | None = None
    notify_on_skipped: bool | None = None
    pre_hooks: list[str] = Field(default_factory=list)
    post_hooks: list[str] = Field(default_factory=list)
    on_error_hooks: list[str] = Field(default_factory=list)

    @field_validator("schedule", mode="before")
    @classmethod
    def validate_cron(cls, v: object) -> object:
        """Validiert den Cron-Ausdruck.

        Leere oder whitespace-only Eingaben werden zu None normalisiert
        (= kein Schedule).

        Args:
            v: Der zu prüfende Cron-Ausdruck.

        Returns:
            `None`, falls `v` `None` oder ein leerer/whitespace-only String ist;
            andernfalls den unveränderten Wert bei Gültigkeit.

        Raises:
            ValueError: Wenn der Ausdruck kein gültiges Cron-Format hat.
        """
        return _validate_cron_schedule(
            v,
            example="'0 3 * * *' (daily at 03:00) or '0 2 * * 1' (Mondays at 02:00)",
        )

    @field_validator("pre_hooks", "post_hooks", "on_error_hooks")
    @classmethod
    def validate_hook_commands(cls, v: list[str]) -> list[str]:
        return _validate_hook_commands(v)

    @field_validator("steps")
    @classmethod
    def validate_steps(cls, v: list[str]) -> list[str]:
        """Prüft Step-Format und Duplikate.

        Args:
            v: Die Liste der Workflow-Steps.

        Returns:
            Die unveränderte Liste bei Gültigkeit.

        Raises:
            ValueError: Wenn ein Step ungültiges Format hat oder doppelt vorkommt.
        """
        seen: set[str] = set()
        for step in v:
            if step in seen:
                raise ValueError(
                    f"Workflow steps must not contain duplicates (duplicate: {step!r})"
                )
            seen.add(step)
        for step in v:
            if not _STEP_RE.fullmatch(step):
                raise ValueError(
                    f"Invalid step format: {step!r}. "
                    "Expected 'backup.<name>', 'backup.<name>.backup', "
                    "'backup.<name>.retention', 'backup.<name>.cleanup', or 'rclone.<name>'."
                )
        return v


class RawJobBackupSectionConfig(StrictConfigModel):
    """Backup-Defaults und Backup-Tasks unter ``[jobs.<job>.backup]``."""

    password: str | None = Field(None, min_length=1)
    password_env: str | None = None
    password_file: str | None = None
    backup_timeout: int | None = Field(None, ge=1)
    retention: bool | None = None
    cleanup: bool | None = None
    auto_init: bool | None = None
    keep_last: int | None = Field(None, ge=1)
    keep_hourly: int | None = Field(None, ge=1)
    keep_daily: int | None = Field(None, ge=1)
    keep_weekly: int | None = Field(None, ge=1)
    keep_monthly: int | None = Field(None, ge=1)
    keep_yearly: int | None = Field(None, ge=1)
    keep_within: str | None = None
    keep_within_hourly: str | None = None
    keep_within_daily: str | None = None
    keep_within_weekly: str | None = None
    keep_within_monthly: str | None = None
    keep_within_yearly: str | None = None
    exclude: list[str] | None = None
    exclude_files: list[str] | None = None
    exclude_caches: bool | None = None
    one_file_system: bool | None = None
    extra_restic_backup_args: list[str] | None = None
    extra_restic_forget_args: list[str] | None = None
    extra_restic_prune_args: list[str] | None = None
    tasks: dict[str, RawBackupConfig] = Field(default_factory=dict)

    @field_validator("password_env")
    @classmethod
    def validate_env_var_name(cls, v: str | None) -> str | None:
        return _validate_env_var_name(v)

    @model_validator(mode="before")
    @classmethod
    def normalize_tasks(cls, data: object) -> object:
        return _normalize_dynamic_task_section(
            data,
            default_keys=set(cls.model_fields) - {"tasks"},
            section_name="[jobs.<job>.backup]",
        )

    @model_validator(mode="after")
    def validate_names_and_passwords(self) -> "RawJobBackupSectionConfig":
        _validate_config_names("backup", self.tasks, reserved={"rclone"})
        if self.password is not None and self.password_env is not None:
            raise ValueError("password and password_env are mutually exclusive")
        if self.password is not None and self.password_file is not None:
            raise ValueError("password and password_file are mutually exclusive")
        if self.password_env is not None and self.password_file is not None:
            raise ValueError("password_env and password_file are mutually exclusive")
        return self

    @field_validator(*_KEEP_WITHIN_FIELD_NAMES)
    @classmethod
    def validate_keep_within(cls, v: str | None) -> str | None:
        return _validate_keep_within(v)

    @field_validator(
        "extra_restic_backup_args",
        "extra_restic_forget_args",
        "extra_restic_prune_args",
    )
    @classmethod
    def validate_extra_args(cls, v: list[str] | None) -> list[str] | None:
        return _validate_extra_args(v)


class RawJobRcloneSectionConfig(StrictConfigModel):
    """Rclone-Defaults und Rclone-Tasks unter ``[jobs.<job>.rclone]``."""

    rclone_timeout: int | None = Field(None, ge=1)
    sync_delete: bool | None = None
    exclude: list[str] | None = None
    filter_from: str | None = None
    transfers: int | None = Field(None, ge=1)
    checkers: int | None = Field(None, ge=1)
    bwlimit: str | None = None
    extra_rclone_args: list[str] | None = None
    tasks: dict[str, RawRcloneSyncTaskConfig] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def normalize_tasks(cls, data: object) -> object:
        return _normalize_dynamic_task_section(
            data,
            default_keys=set(cls.model_fields) - {"tasks"},
            section_name="[jobs.<job>.rclone]",
        )

    @model_validator(mode="after")
    def validate_task_names(self) -> "RawJobRcloneSectionConfig":
        _validate_config_names("rclone task", self.tasks)
        return self

    @field_validator("extra_rclone_args")
    @classmethod
    def validate_extra_args(cls, v: list[str] | None) -> list[str] | None:
        return _validate_extra_args(v)

    @field_validator("bwlimit", mode="before")
    @classmethod
    def coerce_empty_bwlimit(cls, v: object) -> object:
        return _coerce_empty_string_to_none(v)

    @field_validator("bwlimit")
    @classmethod
    def validate_bwlimit(cls, v: str | None) -> str | None:
        return _validate_bwlimit(v)


def _default_raw_job_backup_section_config() -> RawJobBackupSectionConfig:
    return RawJobBackupSectionConfig.model_validate({})


def _default_raw_job_rclone_section_config() -> RawJobRcloneSectionConfig:
    return RawJobRcloneSectionConfig.model_validate({})


class RawJobConfig(StrictConfigModel):
    """Konfiguration für einen Job ([jobs.<name>]).

    Der Job-Name ergibt sich aus dem Dict-Key, nicht aus einem eigenen Feld.

    Attributes:
        backup: Backup-Defaults und benannte Backup-Tasks.
        rclone: Rclone-Defaults und benannte Rclone-Tasks.
        workflow: Benannte Workflows mit je eigenem Schedule.
        hook_timeout: Hook-Timeout, vererbt global → job → task/workflow.
        notify_on_*: Benachrichtigungs-Trigger, vererbt global → job → task/workflow.
    """

    backup: RawJobBackupSectionConfig = Field(
        default_factory=_default_raw_job_backup_section_config
    )
    rclone: RawJobRcloneSectionConfig = Field(
        default_factory=_default_raw_job_rclone_section_config
    )
    workflow: dict[str, RawWorkflowConfig] = Field(default_factory=dict)
    hook_timeout: int | None = Field(None, ge=1)
    notify_on_success: bool | None = None
    notify_on_error: bool | None = None
    notify_on_skipped: bool | None = None
    pre_hooks: list[str] = Field(default_factory=list)
    post_hooks: list[str] = Field(default_factory=list)
    on_error_hooks: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_workflow_names(self) -> "RawJobConfig":
        _validate_config_names("workflow", self.workflow, reserved={"rclone"})
        return self

    @field_validator("pre_hooks", "post_hooks", "on_error_hooks")
    @classmethod
    def validate_hook_commands(cls, v: list[str]) -> list[str]:
        return _validate_hook_commands(v)


class RawAppConfig(StrictConfigModel):
    """Haupt-Konfiguration, entspricht dem gesamten TOML-Dokument.

    Attributes:
        global_: Globale Einstellungen (TOML-Key: "global").
        jobs: Dict aller konfigurierten Jobs (Key = Job-Name).
    """

    global_: RawGlobalConfig = Field(alias="global", default_factory=_default_raw_global_config)
    jobs: dict[str, RawJobConfig] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")

    @field_validator("jobs")
    @classmethod
    def validate_job_names(cls, v: dict[str, RawJobConfig]) -> dict[str, RawJobConfig]:
        for key in v:
            if not _NAME_RE.fullmatch(key):
                raise ValueError(
                    f"Invalid job name: {key!r}. "
                    "Job names must match ^[A-Za-z0-9_-]+$ (letters, digits, hyphens, underscores)."
                )
            if key in _RESERVED_JOB_NAMES:
                raise ValueError(
                    f"Invalid job name: {key!r}. "
                    f"{key!r} is reserved and cannot be used as a job name."
                )
        return v
