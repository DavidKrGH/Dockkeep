"""Config-Validierung und -Loading für den Dockkeep."""

import logging
import os
import re
import tomllib
from pathlib import Path

from ..models.config import (
    NotificationEventKind,
    RawAppConfig,
    RawBackupConfig,
    RawJobBackupSectionConfig,
    RawJobConfig,
    RawJobRcloneSectionConfig,
)
from ..models.resolve import resolve_config
from ..models.resolved_config import (
    ResolvedAppConfig,
    ResolvedBackupConfig,
    ResolvedJobConfig,
    ResolvedNotificationsConfig,
)

_RCLONE_REPO_RE = re.compile(r"^rclone:[^:]+:.+$")
_RCLONE_ENDPOINT_RE = re.compile(r"^[^:/]+:.+$")
logger = logging.getLogger(__name__)


def load_raw_config(config_path: str | Path) -> RawAppConfig:
    """Lädt und validiert eine TOML-Konfigurationsdatei.

    Führt folgende Schritte aus:
    1. TOML-Datei einlesen und parsen
    2. Pydantic-Modell erstellen (Feld-Validierung)
    3. ``validate_raw_config()``: strukturelle/syntaktische Regeln auf der
       frisch geparsten Config

    Args:
        config_path: Pfad zur TOML-Konfigurationsdatei.

    Returns:
        Validierte RawAppConfig ohne angewendete Vererbungs-Defaults.

    Raises:
        FileNotFoundError: Wenn die Konfigurationsdatei nicht existiert.
        tomllib.TOMLDecodeError: Wenn die Datei kein gültiges TOML ist.
        pydantic.ValidationError: Wenn die Konfiguration ungültig ist.
        ValueError: Wenn eine Validierungsregel verletzt wird.

    Example:
        >>> config = load_raw_config("/config/config.toml")
        >>> print(config.global_.log_level)
        'debug'
    """
    config_path = Path(config_path)

    with config_path.open("rb") as f:
        raw = tomllib.load(f)

    config = RawAppConfig(**raw)

    validate_raw_config(config)

    return config


def load_config(config_path: str | Path) -> ResolvedAppConfig:
    """Lädt eine TOML-Konfigurationsdatei und löst sie zu einer ResolvedAppConfig auf.

    Führt folgende Schritte aus:
    1. ``load_raw_config()``: TOML → RawAppConfig → Validate
    2. ``resolve_config()``: dreistufige Vererbung (global → job →
       backup/workflow/rclone) zu einer laufzeitfertigen ResolvedAppConfig

    Args:
        config_path: Pfad zur TOML-Konfigurationsdatei.

    Returns:
        Laufzeitfertige ResolvedAppConfig mit aufgelösten Werten für alle
        Jobs, Backups, Workflows und Rclone-Sync-Tasks.

    Raises:
        FileNotFoundError: Wenn die Konfigurationsdatei nicht existiert.
        tomllib.TOMLDecodeError: Wenn die Datei kein gültiges TOML ist.
        pydantic.ValidationError: Wenn die Konfiguration ungültig ist.
        ValueError: Wenn eine Validierungsregel verletzt wird.

    Example:
        >>> config = load_config("/config/config.toml")
        >>> print(config.global_.log_level)
        'debug'
    """
    return load_config_with_raw(config_path)[1]


def load_config_with_raw(config_path: str | Path) -> tuple[RawAppConfig, ResolvedAppConfig]:
    """Wie ``load_config()``, gibt zusätzlich die zugrunde liegende Raw-Config zurück.

    Raw- und Resolved-Config stammen garantiert aus demselben Datei-Parse;
    Aufrufer, die neben den effektiven Werten auch die explizit gesetzten
    Roh-Werte benötigen (z.B. für Herkunfts-Anzeigen), verwenden diese
    Variante statt zweier getrennter Ladevorgänge.

    Args:
        config_path: Pfad zur TOML-Konfigurationsdatei.

    Returns:
        Tupel aus validierter RawAppConfig und laufzeitfertiger
        ResolvedAppConfig.

    Raises:
        FileNotFoundError: Wenn die Konfigurationsdatei nicht existiert.
        tomllib.TOMLDecodeError: Wenn die Datei kein gültiges TOML ist.
        pydantic.ValidationError: Wenn die Konfiguration ungültig ist.
        ValueError: Wenn eine Validierungsregel verletzt wird.
    """
    raw = load_raw_config(config_path)
    config = resolve_config(raw)
    validate_resolved_config(config)
    _log_config_warnings(config)
    return raw, config


def validate_raw_config(raw: RawAppConfig) -> None:
    """Strukturelle/syntaktische Validierung, die keine Vererbung voraussetzt.

    Wird auf der frisch geparsten ``RawAppConfig`` aufgerufen. Prüft Regeln,
    die für beliebige explizit gesetzte Werte auf jeder Ebene unabhängig von
    Vererbung gelten:

    - Control Characters in Pfaden, Repository- und Rclone-Endpunkten
    - Repository-Format: lokaler Pfad (beginnt mit ``/``) oder
      ``rclone:<remote>:<path>``
    - ``password_file``: muss (falls gesetzt) ein absoluter Pfad sein
    - Sources: explizit gesetzte ``sources`` (Backup-Ebene) müssen
      nicht-leere, absolute Pfade ohne Control Characters sein
    - ``source_files``, ``exclude_files`` und ``filter_from`` müssen
      existierende absolute Dateien ohne Control Characters sein
    - Rclone-Source/-Target-Format
    - Workflow-Steps: referenzierte Backups und Rclone-Tasks müssen
      konfiguriert sein

    Args:
        raw: Die geparste, noch nicht mit Defaults gemischte RawAppConfig.

    Raises:
        ValueError: Wenn eine Validierungsregel verletzt wird.

    """
    g = raw.global_
    if g.backup.password_file is not None:
        _validate_existing_file_path("[global.backup]: password_file", g.backup.password_file)
    if g.backup.exclude_files is not None:
        _validate_existing_file_paths("[global.backup]: exclude_files", g.backup.exclude_files)
    if g.rclone.filter_from is not None:
        _validate_existing_file_path("[global.rclone]: filter_from", g.rclone.filter_from)

    for job_name, job in raw.jobs.items():
        _validate_job_backup_defaults_raw(job_name, job.backup)
        _validate_job_rclone_defaults_raw(job_name, job.rclone)
        for backup_name, backup in job.backup.tasks.items():
            _validate_backup_raw(job_name, backup_name, backup)
        _validate_workflow_steps(job_name, job)
        for rclone_name, rclone_task in job.rclone.tasks.items():
            _validate_absolute_path(
                f"[jobs.{job_name}.rclone.{rclone_name}]: source",
                rclone_task.source,
            )
            _validate_no_control_characters(
                f"[jobs.{job_name}.rclone.{rclone_name}]: target", rclone_task.target
            )
            _validate_rclone_target(job_name, rclone_name, rclone_task.target)
            if rclone_task.filter_from is not None:
                _validate_existing_file_path(
                    f"[jobs.{job_name}.rclone.{rclone_name}]: filter_from",
                    rclone_task.filter_from,
                )


def _validate_job_backup_defaults_raw(
    job_name: str, backup_defaults: RawJobBackupSectionConfig
) -> None:
    """Validates raw fields in ``[jobs.<job>.backup]`` defaults."""
    if backup_defaults.password_file is not None:
        _validate_existing_file_path(
            f"[jobs.{job_name}.backup]: password_file", backup_defaults.password_file
        )
    if backup_defaults.exclude_files is not None:
        _validate_existing_file_paths(
            f"[jobs.{job_name}.backup]: exclude_files", backup_defaults.exclude_files
        )


def _validate_job_rclone_defaults_raw(
    job_name: str, rclone_defaults: RawJobRcloneSectionConfig
) -> None:
    """Validates raw fields in ``[jobs.<job>.rclone]`` defaults."""
    if rclone_defaults.filter_from is not None:
        _validate_existing_file_path(
            f"[jobs.{job_name}.rclone]: filter_from", rclone_defaults.filter_from
        )


def _validate_backup_raw(job_name: str, backup_name: str, backup: RawBackupConfig) -> None:
    """Validiert die strukturellen Felder eines einzelnen Backups (Raw-Phase)."""
    backup_path = f"[jobs.{job_name}.backup.{backup_name}]"
    repo = backup.repository
    _validate_no_control_characters(f"{backup_path}: repository", repo)
    if not repo.startswith("/") and not _RCLONE_REPO_RE.match(repo):
        raise ValueError(
            f"{backup_path}: invalid repository format '{repo}'. "
            "Must be an absolute local path (starting with '/') or "
            "a rclone backend path (format: 'rclone:<remote>:<path>')."
        )

    if backup.password_file is not None:
        _validate_existing_file_path(f"{backup_path}: password_file", backup.password_file)

    if backup.sources is not None:
        _validate_sources(f"{backup_path}: sources", backup.sources)
    if backup.source_files is not None:
        _validate_existing_file_paths(f"{backup_path}: source_files", backup.source_files)
    if backup.exclude_files is not None:
        _validate_existing_file_paths(f"{backup_path}: exclude_files", backup.exclude_files)
    for tag in backup.tags:
        _validate_no_control_characters(f"{backup_path}: tags entry", tag)


def _validate_sources(context: str, sources: list[str]) -> None:
    """Validates source paths for the backup task level."""
    for src in sources:
        if not src:
            raise ValueError(f"{context} must not contain empty strings.")
        _validate_absolute_path(f"{context} entry", src)


def _validate_existing_file_paths(context: str, paths: list[str]) -> None:
    """Validates a list of existing file paths from raw config."""
    for path in paths:
        if not path:
            raise ValueError(f"{context} must not contain empty strings.")
        _validate_existing_file_path(f"{context} entry", path)


def _validate_absolute_path(context: str, value: str) -> None:
    """Reject non-absolute paths and control characters."""
    _validate_no_control_characters(context, value)
    if not value.startswith("/"):
        raise ValueError(
            f"{context} {value!r} must be an absolute path (start with '/'). "
            "Relative paths are not supported."
        )


def _validate_existing_file_path(context: str, value: str) -> None:
    """Reject non-absolute paths, control characters and missing files."""
    _validate_absolute_path(context, value)
    if not Path(value).is_file():
        raise ValueError(f"{context} {value!r} does not exist or is not a file.")


def _validate_no_control_characters(context: str, value: str) -> None:
    """Reject control characters in paths and Rclone endpoints."""
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{context} {value!r} contains invalid characters (control characters).")


def _validate_rclone_target(job_name: str, rclone_name: str, target: str) -> None:
    """Validate direct rclone endpoint format for jobs.<job>.rclone.<name>.target."""
    if not _RCLONE_ENDPOINT_RE.fullmatch(target):
        raise ValueError(
            f"[jobs.{job_name}.rclone.{rclone_name}]: target {target!r} must be a rclone endpoint "
            "in '<remote>:<path>' format. Relative or local paths are not supported."
        )


def _validate_workflow_steps(job_name: str, job: RawJobConfig) -> None:
    """Validiert dass alle Workflow-Steps auf existierende Backups/Rclone zeigen."""
    for wf_name, workflow in job.workflow.items():
        for step in workflow.steps:
            parts = step.split(".")
            if parts[0] == "rclone":
                rclone_name = parts[1]
                if rclone_name not in job.rclone.tasks:
                    raise ValueError(
                        f"[jobs.{job_name}.workflow.{wf_name}]: "
                        f"step '{step}' references rclone task '{rclone_name}' "
                        f"which is not defined in [jobs.{job_name}.rclone]"
                    )
            else:
                # Format validated by model: backup.<name> or backup.<name>.<substep>
                backup_name = parts[1]
                if backup_name not in job.backup.tasks:
                    raise ValueError(
                        f"[jobs.{job_name}.workflow.{wf_name}]: "
                        f"step '{step}' references backup '{backup_name}' "
                        f"which is not defined in [jobs.{job_name}.backup.{backup_name}]"
                    )


def validate_resolved_config(config: ResolvedAppConfig) -> None:
    """Validierung, die effektive (aufgelöste) Werte voraussetzt.

    Wird auf einer ``ResolvedAppConfig`` aufgerufen, nachdem
    ``resolve_config()`` die dreistufige Vererbung angewendet hat. Prüft
    Regeln, die nur auf Basis der effektiven Werte sinnvoll sind:

    - ``execution.retention=True`` erfordert mindestens ein gesetztes
      ``retention.keep_*``-Feld
    - fehlende Backup-Eingaben sind kein Hard-Error; Backups ohne effektive
      ``sources`` und ohne ``source_files`` werden von
      ``collect_config_warnings()`` gemeldet
    - ``credentials.password_file``: muss, falls effektiv gesetzt, auf eine
      existierende Datei zeigen

    Args:
        config: Die vollständig aufgelöste ResolvedAppConfig.

    Raises:
        ValueError: Wenn eine Validierungsregel verletzt wird.

    """
    _validate_lock_retry_config(config)
    for job_name, job in config.jobs.items():
        for backup_name, backup in job.backup.items():
            _validate_backup_resolved(job_name, backup_name, backup)
        _validate_workflow_retention(job_name, job)


def _validate_lock_retry_config(config: ResolvedAppConfig) -> None:
    retry_count = config.global_.lock_retry_count or 0
    if retry_count >= 1 and config.global_.lock_retry_delay is None:
        raise ValueError("[global]: lock_retry_count >= 1 requires lock_retry_delay to be set.")


def _backup_has_keep_policy(backup: ResolvedBackupConfig) -> bool:
    retention = backup.retention
    keep_fields = (
        retention.keep_last,
        retention.keep_hourly,
        retention.keep_daily,
        retention.keep_weekly,
        retention.keep_monthly,
        retention.keep_yearly,
        retention.keep_within,
        retention.keep_within_hourly,
        retention.keep_within_daily,
        retention.keep_within_weekly,
        retention.keep_within_monthly,
        retention.keep_within_yearly,
    )
    return any(field is not None for field in keep_fields)


def _validate_workflow_retention(job_name: str, job: ResolvedJobConfig) -> None:
    """Erzwinge keep_* für Backups, die ein Workflow per Retention-Schritt aufruft.

    Ein Workflow-Step ``backup.<name>.retention`` (oder ``backup.<name>`` bei
    ``retention=true``, bereits abgedeckt) führt ``restic forget`` aus. Ohne
    keep_*-Policy verweigert restic die Ausführung (``Fatal: no policy was
    specified``) und lässt den Workflow mit kryptischer Meldung scheitern.
    Diese Regel meldet das Problem früh als klaren Config-Fehler.
    """
    for workflow_name, workflow in job.workflows.items():
        for step in workflow.steps:
            parts = step.split(".")
            if len(parts) != 3 or parts[0] != "backup" or parts[2] != "retention":
                continue
            backup_name = parts[1]
            backup = job.backup.get(backup_name)
            if backup is not None and not _backup_has_keep_policy(backup):
                raise ValueError(
                    f"Job '{job_name}', workflow '{workflow_name}': step "
                    f"'{step}' runs 'restic forget' but backup '{backup_name}' has no "
                    f"keep_* policy. Set at least one keep_* field in "
                    f"[jobs.{job_name}.backup.{backup_name}], [jobs.{job_name}.backup], "
                    f"or [global.backup]."
                )


def _validate_backup_resolved(
    job_name: str, backup_name: str, backup: ResolvedBackupConfig
) -> None:
    """Validiert die effektiven Werte eines einzelnen Backups (Resolved-Phase)."""
    if backup.execution.retention and not _backup_has_keep_policy(backup):
        raise ValueError(
            f"Job '{job_name}', backup '{backup_name}': retention=true requires at least one "
            f"keep_* field (set in [jobs.{job_name}.backup.{backup_name}], "
            f"[jobs.{job_name}.backup], or [global.backup])"
        )

    password_file = backup.credentials.password_file
    if password_file is not None and not Path(password_file).is_file():
        raise ValueError(
            f"[jobs.{job_name}.backup.{backup_name}]: effective password_file "
            f"{password_file!r} does not exist or is not a file. "
            f"Set it in [jobs.{job_name}.backup.{backup_name}], "
            f"[jobs.{job_name}.backup], or [global.backup]."
        )


def collect_config_warnings(config: ResolvedAppConfig) -> list[str]:
    """Return non-fatal warnings for a resolved config.

    Args:
        config: Die vollständig aufgelöste ResolvedAppConfig.

    Returns:
        Liste menschenlesbarer Warnungen (kann leer sein).
    """
    warnings: list[str] = []
    for job_name, job in config.jobs.items():
        for backup_name, backup in job.backup.items():
            if not backup.input.sources and not backup.input.source_files:
                warnings.append(
                    f"Job '{job_name}', backup '{backup_name}': no backup inputs "
                    "configured. Set 'sources' or 'source_files', otherwise Restic "
                    "will only report the error at run time."
                )

            credentials = backup.credentials
            if (
                credentials.password is None
                and credentials.password_file is None
                and credentials.password_env is not None
            ):
                warnings.append(
                    f"Job '{job_name}', backup '{backup_name}': password_env "
                    f"'{credentials.password_env}' is set, but the environment variable "
                    "was not defined when the config was loaded. Restic will only "
                    "report the missing repository key at run time."
                )
            if (
                credentials.password is None
                and credentials.password_file is None
                and credentials.password_env is None
            ):
                warnings.append(
                    f"Job '{job_name}', backup '{backup_name}': no Restic password "
                    "source configured. Set 'password_env', 'password_file', or "
                    "'password', otherwise Restic will only report the missing "
                    "repository key at run time."
                )

    has_provider = (
        config.global_.notifications.mail is not None
        or config.global_.notifications.pushover is not None
    )
    warnings.extend(_notification_provider_env_warnings(config))

    if not has_provider and _notifications_requested(config):
        warnings.append(
            "Notifications are enabled, but no provider is configured. "
            "Notification events are logged but not sent. "
            "Global triggers are configured under [global]. "
            "Configure [global.notifications.mail] or "
            "[global.notifications.pushover] to send notifications."
        )
    elif has_provider:
        warnings.extend(_notification_routing_warnings(config))

    return warnings


def _notification_provider_env_warnings(config: ResolvedAppConfig) -> list[str]:
    warnings: list[str] = []
    providers = config.global_.notifications

    if providers.mail is not None:
        for field, env_name in (
            ("username_env", providers.mail.username_env),
            ("password_env", providers.mail.password_env),
        ):
            if _env_var_missing(env_name):
                warnings.append(
                    "Mail notification provider: "
                    f"{field} '{env_name}' is configured, but the environment variable "
                    "was not defined or is empty when the config was loaded. "
                    "Notifications will fail at send time until it is set."
                )

    if providers.pushover is not None:
        for field, env_name in (
            ("token_env", providers.pushover.token_env),
            ("user_key_env", providers.pushover.user_key_env),
        ):
            if _env_var_missing(env_name):
                warnings.append(
                    "Pushover notification provider: "
                    f"{field} '{env_name}' is configured, but the environment variable "
                    "was not defined or is empty when the config was loaded. "
                    "Notifications will fail at send time until it is set."
                )

    return warnings


def _env_var_missing(env_name: object) -> bool:
    return isinstance(env_name, str) and bool(env_name) and not os.environ.get(env_name)


def _log_config_warnings(config: ResolvedAppConfig) -> None:
    """Log non-fatal validation warnings."""
    for warning in collect_config_warnings(config):
        logger.warning(warning)


def _notifications_requested(config: ResolvedAppConfig) -> bool:
    return bool(_requested_event_kinds(config))


def _requested_event_kinds(config: ResolvedAppConfig) -> set[NotificationEventKind]:
    """Return the event kinds the effective config can raise at all.

    Der periodische Bericht hängt am globalen ``report_schedule``, die übrigen
    Ereignisarten an den aufgelösten ``notify_on_*``-Triggern der Tasks.
    """
    kinds: set[NotificationEventKind] = set()
    if config.global_.notifications.report_schedule is not None:
        kinds.add("report")

    for job in config.jobs.values():
        for notifications in (
            *(backup.notifications for backup in job.backup.values()),
            *(workflow.notifications for workflow in job.workflows.values()),
            *(rclone.notifications for rclone in job.rclone.values()),
        ):
            kinds.update(_requested_task_event_kinds(notifications))

    return kinds


def _requested_task_event_kinds(
    notifications: ResolvedNotificationsConfig,
) -> set[NotificationEventKind]:
    kinds: set[NotificationEventKind] = set()
    if notifications.notify_on_success:
        kinds.add("success")
    if notifications.notify_on_error:
        kinds.add("error")
    if notifications.notify_on_skipped:
        kinds.add("skipped")
    return kinds


_EVENT_KIND_TRIGGERS: dict[NotificationEventKind, str] = {
    "success": "'notify_on_success' is enabled",
    "error": "'notify_on_error' is enabled",
    "skipped": "'notify_on_skipped' is enabled",
    "report": "a periodic run report is scheduled via 'report_schedule'",
}


def _notification_routing_warnings(config: ResolvedAppConfig) -> list[str]:
    """Return warnings for raised event kinds that no active provider carries.

    Greift nur bei mindestens einem konfigurierten Provider; der Fall ganz ohne
    Provider ist bereits durch eine eigene Warning abgedeckt.
    """
    providers = config.global_.notifications
    active = [p for p in (providers.mail, providers.pushover) if p is not None]
    requested = _requested_event_kinds(config)

    warnings: list[str] = []
    for kind in _EVENT_KIND_TRIGGERS:
        if kind not in requested:
            continue
        if any(p.events is None or kind in p.events for p in active):
            continue
        warnings.append(
            f"Notification routing: {_EVENT_KIND_TRIGGERS[kind]}, but no configured "
            f"provider carries '{kind}' events. These notifications are logged but "
            f"not sent. Add '{kind}' to the 'events' list of "
            "[global.notifications.mail] or [global.notifications.pushover], "
            "or remove the list to carry every event."
        )

    return warnings
