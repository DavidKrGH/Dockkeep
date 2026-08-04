"""Pure Resolver: ``RawAppConfig`` -> ``ResolvedAppConfig``.

Dieses Modul implementiert ``resolve_config()``, den einzigen Ort, an dem die
dreistufige Config-Vererbung (``global -> job -> backup/workflow/rclone``)
ausgewertet wird.

``resolve_config()`` mutiert die übergebene ``RawAppConfig`` (oder ein
verschachteltes Objekt darin) nicht. Es liest ausschließlich Werte und erzeugt
neue ``Resolved*``-Objekte.

Die eigentliche Vererbungslogik (Scalar-Override, List-Override,
Password-Choice) ist generisch und wird von ``src.models.config_fields.
CONFIG_FIELDS`` parametrisiert: für jede Ziel-Ebene (``job``, ``backup``,
``workflow``, ``rclone``) werden die zugehörigen Felddefinitionen iteriert und
anhand ihrer ``inheritance``-Art mit den passenden generischen Hilfsfunktionen
aufgelöst. Die Roh-Werte je Ebene werden anhand desselben Feldschemas
generisch eingesammelt. Die verbleibende feldspezifische Arbeit beschränkt
sich auf das Verteilen der aufgelösten Werte in die fachlichen
``Resolved*``-Gruppen (``_assemble_*()``).
"""

import os
from typing import Any, Literal

from src.models.config import RawAppConfig
from src.models.config_fields import ConfigField, Level, fields_for_level
from src.models.resolved_config import (
    ResolvedAppConfig,
    ResolvedBackendOptions,
    ResolvedBackupConfig,
    ResolvedCredentials,
    ResolvedExecutionConfig,
    ResolvedFiltersConfig,
    ResolvedGlobalConfig,
    ResolvedGlobalNotificationProvidersConfig,
    ResolvedHooksConfig,
    ResolvedInputConfig,
    ResolvedJobConfig,
    ResolvedMailNotificationConfig,
    ResolvedNotificationsConfig,
    ResolvedPushoverNotificationConfig,
    ResolvedRcloneOptionsConfig,
    ResolvedRcloneSyncTaskConfig,
    ResolvedResticBackendOptions,
    ResolvedRetentionConfig,
    ResolvedTimeoutsConfig,
    ResolvedWorkflowConfig,
)

#: Raw-Werte einer Config-Ebene, indiziert nach ``ConfigField.key``.
RawValues = dict[str, Any]

#: Bereits aufgelöste Werte einer Ziel-Ebene, indiziert nach
#: ``ConfigField.resolved_key`` innerhalb der jeweiligen ``capability``-Gruppe.
ResolvedValues = dict[str, dict[str, Any]]


def _resolve_scalar(field: ConfigField, child_raw: RawValues, parent_resolved: RawValues) -> Any:
    """Löst ein ``scalar_override``-Feld auf.

    ``None`` auf Kind-Ebene erbt den (bereits aufgelösten) Elternwert; ein
    gesetzter Wert (inklusive ``False``) stoppt die Vererbung.

    Args:
        field: Die Felddefinition (``inheritance == "scalar_override"``).
        child_raw: Roh-Werte der Kind-Ebene, indiziert nach Raw-Feldname.
        parent_resolved: Bereits aufgelöste Werte der Eltern-Ebene,
            indiziert nach ``resolved_key``.

    Returns:
        Den effektiven Wert für ``field.resolved_key`` auf Kind-Ebene.
    """
    value = child_raw.get(field.key)
    if value is not None:
        return value
    parent_lookup_key = field.parent_key or field.resolved_key
    parent_value = parent_resolved.get(parent_lookup_key, field.default)
    if parent_value is None:
        return field.default
    return parent_value


def _resolve_list(
    field: ConfigField, child_raw: RawValues, parent_resolved: RawValues
) -> list[Any]:
    """Löst ein ``list_override``-Feld auf.

    ``None`` auf Kind-Ebene erbt die Elternliste als Kopie; ``[]`` stoppt die
    Vererbung explizit und liefert eine leere Liste. Geerbte Listen werden
    immer kopiert, damit Eltern- und Kind-Resolved-Objekte keine gemeinsame
    Listenreferenz teilen.

    Args:
        field: Die Felddefinition (``inheritance == "list_override"``).
        child_raw: Roh-Werte der Kind-Ebene, indiziert nach Raw-Feldname.
        parent_resolved: Bereits aufgelöste Werte der Eltern-Ebene,
            indiziert nach ``resolved_key``.

    Returns:
        Eine neue, konkrete Liste für ``field.resolved_key`` auf Kind-Ebene.
    """
    value = child_raw.get(field.key)
    if value is not None:
        return list(value)
    parent_lookup_key = field.parent_key or field.resolved_key
    parent_value = parent_resolved.get(parent_lookup_key)
    if parent_value is None:
        parent_value = field.default
    return list(parent_value)  # type: ignore[arg-type]


def _resolve_password_choice(
    fields: tuple[ConfigField, ConfigField, ConfigField],
    child_raw: RawValues,
    parent_resolved: RawValues,
) -> dict[str, str | None]:
    """Löst das ``password``/``password_env``/``password_file``-Tripel auf.

    Das Kind erbt das gesamte Tripel vom Elternwert nur, wenn auf Kind-Ebene
    keines der drei Felder gesetzt ist. Andernfalls werden genau die auf
    Kind-Ebene gesetzten Werte verwendet (die anderen beiden bleiben ``None``).

    Args:
        fields: Die drei ``ConfigField``-Definitionen für ``password``,
            ``password_env`` und ``password_file`` auf der Kind-Ebene
            (``inheritance == "password_choice"``).
        child_raw: Roh-Werte der Kind-Ebene, indiziert nach Raw-Feldname.
        parent_resolved: Bereits aufgelöste Credential-Werte der Eltern-Ebene,
            indiziert nach ``resolved_key`` (``"password"``,
            ``"password_env"``, ``"password_file"``).

    Returns:
        Dict mit den drei aufgelösten Werten, indiziert nach
        ``resolved_key``.
    """
    child_values = {f.resolved_key: child_raw.get(f.key) for f in fields}
    if any(v is not None for v in child_values.values()):
        return child_values
    return {f.resolved_key: parent_resolved.get(f.parent_key or f.resolved_key) for f in fields}


def _resolve_level(
    level: Level,
    child_raw: RawValues,
    parent_resolved_by_parent_level: dict[Level, RawValues],
) -> ResolvedValues:
    """Löst alle vererbbaren Felder einer Ziel-Ebene generisch auf.

    Iteriert ``CONFIG_FIELDS`` für ``level`` und wendet je Feld die zu
    ``field.inheritance`` passende generische Hilfsfunktion an. Felder mit
    ``inheritance == "none"`` werden hier nicht behandelt (siehe
    ``_assemble_*``-Funktionen für die unveränderten "eigenen" Felder).

    Args:
        level: Die Ziel-Ebene (``"job"``, ``"backup"``, ``"workflow"`` oder
            ``"rclone"``).
        child_raw: Roh-Werte der Kind-Ebene, indiziert nach Raw-Feldname.
        parent_resolved_by_parent_level: Bereits aufgelöste Werte je
            Eltern-Ebene (``ConfigField.parent_level``), jeweils indiziert
            nach ``resolved_key``.

    Returns:
        Verschachteltes Dict: ``capability`` -> ``resolved_key`` -> Wert.
    """
    result: ResolvedValues = {}
    password_groups: dict[tuple[str, Level | None], list[ConfigField]] = {}

    for field in fields_for_level(level):
        capability = field.capability
        bucket = result.setdefault(capability, {})

        if field.inheritance == "none":
            continue

        parent_resolved = (
            parent_resolved_by_parent_level.get(field.parent_level, {})
            if field.parent_level is not None
            else {}
        )

        if field.inheritance == "scalar_override":
            bucket[field.resolved_key] = _resolve_scalar(field, child_raw, parent_resolved)
        elif field.inheritance == "list_override":
            bucket[field.resolved_key] = _resolve_list(field, child_raw, parent_resolved)
        elif field.inheritance == "password_choice":
            password_groups.setdefault((capability, field.parent_level), []).append(field)
        else:  # pragma: no cover - exhaustive InheritanceKind
            raise AssertionError(f"Unhandled inheritance kind: {field.inheritance!r}")

    for (capability, parent_level), fields in password_groups.items():
        if len(fields) != 3:  # pragma: no cover - schema invariant
            raise AssertionError(
                f"Expected exactly 3 password_choice fields for {capability!r}, got {len(fields)}"
            )
        parent_resolved = (
            parent_resolved_by_parent_level.get(parent_level, {})
            if parent_level is not None
            else {}
        )
        ordered = tuple(sorted(fields, key=lambda f: f.resolved_key))
        triple = (ordered[0], ordered[1], ordered[2])
        resolved = _resolve_password_choice(triple, child_raw, parent_resolved)
        result.setdefault(capability, {}).update(resolved)

    return result


def _raw_values(model: object, level: Level) -> RawValues:
    """Liest alle im Feldschema bekannten Roh-Werte einer Zielebene."""
    return {field.key: getattr(model, field.key) for field in fields_for_level(level)}


def _parent_raw_values(model: object, child_level: Level, parent_level: Level) -> RawValues:
    """Liest Elternwerte in den Namen, unter denen das Kind sie auflöst."""
    return {
        field.parent_key or field.resolved_key: getattr(model, field.parent_key or field.key)
        for field in fields_for_level(child_level)
        if field.inheritance != "none" and field.parent_level == parent_level
    }


def _assemble_execution(resolved: ResolvedValues) -> ResolvedExecutionConfig:
    bucket = resolved.get("execution", {})
    return ResolvedExecutionConfig(
        retention=bool(bucket.get("retention", False)),
        cleanup=bool(bucket.get("cleanup", False)),
        auto_init=bool(bucket.get("auto_init", False)),
    )


def _assemble_credentials(resolved: ResolvedValues) -> ResolvedCredentials:
    bucket = resolved.get("credentials", {})
    password = bucket.get("password")
    password_env = bucket.get("password_env")
    if password is None and password_env is not None:
        password = os.environ.get(password_env) or None
    return ResolvedCredentials(
        password=password,
        password_env=password_env,
        password_file=bucket.get("password_file"),
    )


def _assemble_input(resolved: ResolvedValues) -> ResolvedInputConfig:
    bucket = resolved.get("input", {})
    return ResolvedInputConfig(
        sources=list(bucket.get("sources", [])),
        source_files=list(bucket.get("source_files", [])),
    )


def _assemble_filters(resolved: ResolvedValues) -> ResolvedFiltersConfig:
    bucket = resolved.get("filters", {})
    return ResolvedFiltersConfig(
        exclude=list(bucket.get("exclude", [])),
        exclude_files=list(bucket.get("exclude_files", [])),
    )


def _assemble_retention(resolved: ResolvedValues) -> ResolvedRetentionConfig:
    bucket = resolved.get("retention", {})
    return ResolvedRetentionConfig(
        keep_last=bucket.get("keep_last"),
        keep_hourly=bucket.get("keep_hourly"),
        keep_daily=bucket.get("keep_daily"),
        keep_weekly=bucket.get("keep_weekly"),
        keep_monthly=bucket.get("keep_monthly"),
        keep_yearly=bucket.get("keep_yearly"),
        keep_within=bucket.get("keep_within"),
        keep_within_hourly=bucket.get("keep_within_hourly"),
        keep_within_daily=bucket.get("keep_within_daily"),
        keep_within_weekly=bucket.get("keep_within_weekly"),
        keep_within_monthly=bucket.get("keep_within_monthly"),
        keep_within_yearly=bucket.get("keep_within_yearly"),
    )


def _assemble_notifications(resolved: ResolvedValues) -> ResolvedNotificationsConfig:
    bucket = resolved.get("notifications", {})
    return ResolvedNotificationsConfig(
        notify_on_success=bool(bucket.get("notify_on_success", False)),
        notify_on_error=bool(bucket.get("notify_on_error", False)),
        notify_on_skipped=bool(bucket.get("notify_on_skipped", False)),
    )


def _assemble_timeouts(resolved: ResolvedValues) -> ResolvedTimeoutsConfig:
    bucket = resolved.get("timeouts", {})
    return ResolvedTimeoutsConfig(
        backup_timeout=bucket.get("backup_timeout"),
        rclone_timeout=bucket.get("rclone_timeout"),
        hook_timeout=bucket.get("hook_timeout"),
    )


def _assemble_restic_backend_options(resolved: ResolvedValues) -> ResolvedResticBackendOptions:
    bucket = resolved.get("backend_options", {})
    return ResolvedResticBackendOptions(
        exclude_caches=bucket.get("exclude_caches"),
        one_file_system=bucket.get("one_file_system"),
        extra_backup_args=list(bucket.get("extra_backup_args", [])),
        extra_forget_args=list(bucket.get("extra_forget_args", [])),
        extra_prune_args=list(bucket.get("extra_prune_args", [])),
    )


def _assemble_rclone_options(resolved: ResolvedValues) -> ResolvedRcloneOptionsConfig:
    bucket = resolved.get("rclone_options", {})
    return ResolvedRcloneOptionsConfig(
        transfers=bucket.get("transfers"),
        checkers=bucket.get("checkers"),
        bwlimit=bucket.get("bwlimit"),
        extra_args=list(bucket.get("extra_args", [])),
    )


def _assemble_backend(backend: str) -> Literal["restic"]:
    """Gibt den effektiven Backend-Namen zurück."""
    if backend != "restic":
        raise ValueError(f"Unsupported backup backend: {backend!r}")
    return "restic"


def _assemble_hooks(raw: RawValues) -> ResolvedHooksConfig:
    return ResolvedHooksConfig(
        pre_hooks=list(raw.get("pre_hooks") or []),
        post_hooks=list(raw.get("post_hooks") or []),
        on_error_hooks=list(raw.get("on_error_hooks") or []),
    )


def resolve_config(raw: RawAppConfig) -> ResolvedAppConfig:
    """Wandelt eine ``RawAppConfig`` in eine laufzeitfertige ``ResolvedAppConfig``.

    Implementiert die dreistufige Vererbung ``global -> job -> backup/
    workflow/rclone`` rein
    funktional: ``raw`` und alle darin verschachtelten Objekte werden nicht
    mutiert. Die eigentliche Scalar-/List-/Password-Choice-Vererbungslogik
    wird generisch über ``src.models.config_fields.CONFIG_FIELDS`` ausgeführt
    (siehe ``_resolve_level()``); diese Funktion sammelt nur die Roh-Werte
    je Ebene ein und verteilt die aufgelösten Werte in die fachlichen
    ``Resolved*``-Gruppen.

    Args:
        raw: Die geparste und syntaktisch validierte Raw-Config.

    Returns:
        Eine neue ``ResolvedAppConfig`` mit effektiven Werten für alle Jobs,
        Backups, Workflows und Rclone-Sync-Tasks.
    """
    g = raw.global_

    global_resolved = ResolvedGlobalConfig(
        log_level=g.log_level,
        log_retention_days=g.log_retention_days,
        lock_retry_count=g.lock_retry_count,
        lock_retry_delay=g.lock_retry_delay,
        notifications=ResolvedGlobalNotificationProvidersConfig(
            report_schedule=g.notifications.report_schedule,
            mail=(
                ResolvedMailNotificationConfig.model_validate(g.notifications.mail.model_dump())
                if g.notifications.mail is not None
                else None
            ),
            pushover=(
                ResolvedPushoverNotificationConfig.model_validate(
                    g.notifications.pushover.model_dump()
                )
                if g.notifications.pushover is not None
                else None
            ),
        ),
    )

    # "Eltern"-Resolved-Dicts für die job-Ebene: eine pro virtueller
    # global.*-Container-Ebene, jeweils bereits in resolved_key-Namen.
    global_backup_resolved = _parent_raw_values(g.backup, "job.backup", "global.backup")
    global_rclone_resolved = _parent_raw_values(g.rclone, "job.rclone", "global.rclone")
    global_top_resolved = _parent_raw_values(g, "job", "global")

    jobs: dict[str, ResolvedJobConfig] = {}
    for job_name, job in raw.jobs.items():
        job_raw = _raw_values(job, "job")

        job_resolved_by_parent: dict[Level, RawValues] = {
            "global": global_top_resolved,
        }
        job_resolved = _resolve_level("job", job_raw, job_resolved_by_parent)

        job_backup_raw = _raw_values(job.backup, "job.backup")
        job_backup_resolved = _resolve_level(
            "job.backup",
            job_backup_raw,
            {"global.backup": global_backup_resolved},
        )
        job_rclone_raw = _raw_values(job.rclone, "job.rclone")
        job_rclone_resolved = _resolve_level(
            "job.rclone",
            job_rclone_raw,
            {"global.rclone": global_rclone_resolved},
        )

        backups: dict[str, ResolvedBackupConfig] = {}
        for backup_name, backup in job.backup.tasks.items():
            backup_raw = _raw_values(backup, "backup")
            backup_resolved_by_parent: dict[Level, RawValues] = {
                "job": _flatten(job_resolved),
                "job.backup": _flatten(job_backup_resolved),
            }
            backup_resolved = _resolve_level("backup", backup_raw, backup_resolved_by_parent)
            backup_resolved.setdefault("input", {})
            backup_resolved["input"]["sources"] = (
                list(backup.sources) if backup.sources is not None else []
            )
            backup_resolved["input"]["source_files"] = (
                list(backup.source_files) if backup.source_files is not None else []
            )

            backups[backup_name] = ResolvedBackupConfig(
                repository=backup.repository,
                schedule=backup.schedule,
                tags=list(backup.tags),
                backend=_assemble_backend(backup.backend),
                credentials=_assemble_credentials(backup_resolved),
                input=_assemble_input(backup_resolved),
                filters=_assemble_filters(backup_resolved),
                retention=_assemble_retention(backup_resolved),
                execution=_assemble_execution(backup_resolved),
                hooks=_assemble_hooks(backup_raw),
                notifications=_assemble_notifications(backup_resolved),
                timeouts=_assemble_timeouts(backup_resolved),
                backend_options=ResolvedBackendOptions(
                    restic=_assemble_restic_backend_options(backup_resolved)
                ),
            )

        workflows: dict[str, ResolvedWorkflowConfig] = {}
        for workflow_name, workflow in job.workflow.items():
            workflow_raw = _raw_values(workflow, "workflow")
            workflow_resolved_by_parent: dict[Level, RawValues] = {"job": _flatten(job_resolved)}
            workflow_resolved = _resolve_level(
                "workflow", workflow_raw, workflow_resolved_by_parent
            )

            workflows[workflow_name] = ResolvedWorkflowConfig(
                schedule=workflow.schedule,
                steps=list(workflow.steps),
                hooks=_assemble_hooks(workflow_raw),
                notifications=_assemble_notifications(workflow_resolved),
                timeouts=_assemble_timeouts(workflow_resolved),
            )

        rclone_tasks: dict[str, ResolvedRcloneSyncTaskConfig] = {}
        for rclone_name, rclone in job.rclone.tasks.items():
            rclone_raw = _raw_values(rclone, "rclone")
            rclone_resolved_by_parent: dict[Level, RawValues] = {
                "job": _flatten(job_resolved),
                "job.rclone": _flatten(job_rclone_resolved),
            }
            rclone_resolved = _resolve_level("rclone", rclone_raw, rclone_resolved_by_parent)
            rclone_options = rclone_resolved.get("rclone_options", {})

            rclone_tasks[rclone_name] = ResolvedRcloneSyncTaskConfig(
                source=rclone.source,
                target=rclone.target,
                sync_delete=rclone_options.get("sync_delete"),
                exclude=list(rclone_options.get("exclude", [])),
                filter_from=rclone_options.get("filter_from"),
                schedule=rclone.schedule,
                hooks=_assemble_hooks(rclone_raw),
                options=_assemble_rclone_options(rclone_resolved),
                notifications=_assemble_notifications(rclone_resolved),
                timeouts=_assemble_timeouts(rclone_resolved),
            )

        jobs[job_name] = ResolvedJobConfig(
            backup=backups,
            rclone=rclone_tasks,
            workflows=workflows,
            hooks=_assemble_hooks(job_raw),
            notifications=_assemble_notifications(job_resolved),
            timeouts=_assemble_timeouts(job_resolved),
        )

    return ResolvedAppConfig(global_=global_resolved, jobs=jobs)


def _flatten(resolved: ResolvedValues) -> RawValues:
    """Flacht ein ``ResolvedValues``-Dict (``capability -> resolved_key -> value``) ab.

    Wird genutzt, um die auf Job-Ebene aufgelösten Werte als "Eltern-Dict"
    an ``_resolve_level()`` für die nächste Ebene (Backup/Workflow/Rclone)
    zu übergeben. Werte sind nach ``resolved_key`` indiziert.

    Args:
        resolved: Verschachteltes Dict aus ``_resolve_level()``.

    Returns:
        Flaches Dict ``resolved_key -> value`` über alle Capabilities.
    """
    flat: RawValues = {}
    for bucket in resolved.values():
        flat.update(bucket)
    return flat
