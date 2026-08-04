"""Domain-Feldschema für die Config-Vererbung (Raw -> Resolved).

Dieses Modul ist die zentrale Quelle der Domain-Vererbungsregeln für die
Config-Pipeline. Es beschreibt für jedes vererbbare oder fachlich relevante
Feld:

- den Feldnamen im Raw-Zielmodell (``key``),
- den Zielfeldnamen im Ziel-TOML bzw. im späteren ``Resolved*``-Model
  (``resolved_key``),
- die Vererbungskette von Eltern- zu Kind-Ebene (``level`` und ``parent_key``),
- die Vererbungsart (``kind``/``inheritance``),
- den finalen Default nach der letzten Vererbungsstufe (``default``),
- die fachliche Gruppe (``capability``), das Backend (``backend``) und
  optionale Editor-Metadaten (``editor_kind``).

Das Schema dokumentiert bewusst nur die Raw-Feldnamen des Ziel-TOML. Entfernte
Legacy-Felder werden hier nicht als führende Domain-Keys modelliert.

Dieses Modul hat absichtlich keine Pydantic-Abhängigkeit. Der Resolver nutzt
``CONFIG_FIELDS`` als zentrale Quelle der Vererbungsregeln; spätere Schritte
können dasselbe Schema für Validation- oder Editor-Anbindung referenzieren.
"""

from dataclasses import dataclass
from typing import Literal

#: Ebenen der dreistufigen Config-Vererbung (oder Top-Level für
#: nicht-vererbte Felder).
Level = Literal[
    "global",
    "global.backup",
    "global.rclone",
    "global.notifications",
    "job",
    "job.backup",
    "job.rclone",
    "backup",
    "workflow",
    "rclone",
]

#: Art des Feldwerts unabhängig von der Vererbung.
FieldKind = Literal[
    "scalar",
    "list",
    "password_choice",
    "backend_option",
    "capability",
]

#: Art, wie ein Wert von der Eltern- auf die Kind-Ebene übertragen wird.
#:
#: - ``"none"``: keine Vererbung; das Feld existiert nur auf dieser Ebene.
#: - ``"scalar_override"``: ``None`` erbt den Elternwert, ein gesetzter Wert
#:   (inkl. explizitem ``False``) stoppt die Vererbung für diese und alle
#:   tieferen Ebenen.
#: - ``"list_override"``: ``None`` erbt die Elternliste (kopiert), ``[]``
#:   stoppt die Vererbung explizit und liefert eine leere Liste.
#: - ``"password_choice"``: Sonderfall für das ``password``/``password_env``/
#:   ``password_file``-Tripel; das Kind erbt nur, wenn keines der drei Felder
#:   gesetzt ist.
InheritanceKind = Literal[
    "none",
    "scalar_override",
    "list_override",
    "password_choice",
]


@dataclass(frozen=True)
class ConfigField:
    """Domain-Felddefinition für ein Config-Feld.

    Attributes:
        key: Feldname im Raw-Zielmodell (z.B. ``"retention"``,
            ``"source_files"``, ``"backup_timeout"``).
        resolved_key: Zielfeldname im Ziel-TOML bzw. im späteren
            ``Resolved*``-Model (z.B. ``"retention"``, ``"source_files"``,
            ``"backup_timeout"``). Gleich ``key``, falls sich der Name nicht
            ändert.
        level: Ebene, auf der dieses Feld als Kind-Wert ausgewertet wird
            (z.B. ``"backup"`` für ein Feld, dessen effektiver Wert auf
            Backup-Ebene benötigt wird).
        parent_key: Feldname auf der Eltern-Ebene, falls abweichend von
            ``key`` (z.B. Raw-``extra_restic_backup_args`` vs.
            Resolved-``extra_backup_args``). ``None`` bedeutet
            derselbe Name wie ``key`` auf der Eltern-Ebene, oder keine
            Eltern-Ebene (``inheritance == "none"``).
        parent_level: Ebene, von der dieses Feld erbt. ``None``, falls
            ``inheritance == "none"`` oder die Eltern-Ebene global/fix ist
            (siehe ``level``-Dokumentation für die jeweilige Kette).
        kind: Grundlegende Werteart (scalar/list/Sonderfälle).
        inheritance: Vererbungsmechanik zwischen ``parent_level`` und
            ``level``.
        default: Finaler Default-Wert nach der letzten Vererbungsstufe (also
            der Wert, den ein ``Resolved*``-Model erhält, wenn auf keiner
            Ebene ein Wert gesetzt wurde). Für ``list``-Felder ist dies
            typischerweise ``()`` (als unveränderliches Default-Tupel; der
            Resolver erzeugt daraus eine neue, kopierte Liste).
        capability: Fachliche Gruppe, der dieses Feld im Resolved-Model
            zugeordnet ist (z.B. ``"retention"``, ``"input"``, ``"filters"``,
            ``"hooks"``, ``"notifications"``, ``"timeouts"``,
            ``"backend_options"``).
        backend: Backend, dem dieses Feld zugeordnet ist (z.B.
            ``"restic"``), oder ``None`` für backend-neutrale Felder.
        editor_kind: Optionale Editor-Metadaten-Kategorie für
            ``config_editor_schema.py`` (z.B. ``"text"``, ``"int"``,
            ``"bool"``, ``"list"``, ``"choice"``, ``"password"``,
            ``"cron"``). ``None``, falls (noch) keine Editor-Anbindung
            vorgesehen ist.
    """

    key: str
    resolved_key: str
    level: Level
    parent_key: str | None = None
    parent_level: Level | None = None
    kind: FieldKind = "scalar"
    inheritance: InheritanceKind = "none"
    default: object = None
    capability: str = ""
    backend: str | None = None
    editor_kind: str | None = None


def _list_field(
    key: str,
    resolved_key: str,
    level: Level,
    parent_level: Level | None,
    capability: str,
    *,
    parent_key: str | None = None,
    backend: str | None = None,
    editor_kind: str | None = "list",
    inheritance: InheritanceKind = "list_override",
) -> ConfigField:
    """Erzeugt eine ``ConfigField``-Definition für ein vererbbares Listenfeld.

    Vererbbare Listenfelder folgen alle demselben Muster: ``None`` erbt die
    Elternliste (kopiert), ``[]`` stoppt die Vererbung, finaler Default ist
    eine leere Liste (als ``()`` kodiert, siehe ``ConfigField.default``).

    Args:
        key: Feldname im Raw-Model.
        resolved_key: Zielfeldname im Resolved-Model.
        level: Ebene, auf der der effektive Wert benötigt wird.
        parent_level: Ebene, von der geerbt wird, oder ``None`` für
            ``inheritance="none"``.
        capability: Fachliche Gruppe im Resolved-Model.
        parent_key: Abweichender Feldname auf der Eltern-Ebene.
        backend: Backend-Zuordnung, falls backend-spezifisch.
        editor_kind: Editor-Metadaten-Kategorie.
        inheritance: Vererbungsmechanik; Default ``"list_override"``.

    Returns:
        Die fertige ``ConfigField``-Instanz mit ``kind="list"`` und
        ``default=()``.
    """
    return ConfigField(
        key=key,
        resolved_key=resolved_key,
        level=level,
        parent_key=parent_key,
        parent_level=parent_level,
        kind="list",
        inheritance=inheritance,
        default=(),
        capability=capability,
        backend=backend,
        editor_kind=editor_kind,
    )


def _scalar_field(
    key: str,
    resolved_key: str,
    level: Level,
    parent_level: Level | None,
    capability: str,
    *,
    default: object = None,
    backend: str | None = None,
    editor_kind: str | None = "text",
    inheritance: InheritanceKind = "scalar_override",
    kind: FieldKind = "scalar",
) -> ConfigField:
    """Erzeugt eine ``ConfigField``-Definition für ein Skalarfeld.

    Args:
        key: Feldname im Raw-Model.
        resolved_key: Zielfeldname im Resolved-Model.
        level: Ebene, auf der der effektive Wert benötigt wird.
        parent_level: Ebene, von der geerbt wird, oder ``None`` für
            ``inheritance="none"``.
        capability: Fachliche Gruppe im Resolved-Model.
        default: Finaler Default-Wert.
        backend: Backend-Zuordnung, falls backend-spezifisch.
        editor_kind: Editor-Metadaten-Kategorie.
        inheritance: Vererbungsmechanik; Default ``"scalar_override"``.
        kind: Grundlegende Werteart; Default ``"scalar"``.

    Returns:
        Die fertige ``ConfigField``-Instanz.
    """
    return ConfigField(
        key=key,
        resolved_key=resolved_key,
        level=level,
        parent_level=parent_level,
        kind=kind,
        inheritance=inheritance,
        default=default,
        capability=capability,
        backend=backend,
        editor_kind=editor_kind,
    )


_GLOBAL_FIELDS: tuple[ConfigField, ...] = (
    ConfigField(
        key="log_level",
        resolved_key="log_level",
        level="global",
        kind="scalar",
        inheritance="none",
        default="debug",
        capability="logging",
        editor_kind="choice",
    ),
    ConfigField(
        key="log_retention_days",
        resolved_key="log_retention_days",
        level="global",
        kind="scalar",
        inheritance="none",
        default=None,
        capability="logging",
        editor_kind="int",
    ),
    *(
        ConfigField(
            key=raw_name,
            resolved_key=raw_name,
            level="global",
            kind="scalar",
            inheritance="none",
            default=default,
            capability="notifications",
            editor_kind="bool",
        )
        for raw_name, default in {
            "notify_on_success": False,
            "notify_on_error": False,
            "notify_on_skipped": False,
        }.items()
    ),
)


# Drei Felder (`password`, `password_env`, `password_file`) werden als ein
# logischer Vererbungs-"Slot" je Ebene betrachtet: das Kind erbt nur, wenn
# KEINES der drei Felder auf Kind-Ebene gesetzt ist. Wir kodieren das hier als
# drei einzelne ConfigField-Einträge mit `kind="password_choice"`, die alle
# denselben `capability`-Namen ("credentials") und dieselbe Vererbungskette
# teilen, damit ein Resolver sie als zusammengehörige Gruppe erkennen kann
# (z.B. über `capability` + `kind` gruppieren).


def _password_choice_fields(level: Level, parent_level: Level | None) -> tuple[ConfigField, ...]:
    """Erzeugt die drei ``password_choice``-Felder für eine Ebene.

    Args:
        level: Ebene, auf der die effektiven Credentials benötigt werden
            (``"job"`` oder ``"backup"``).
        parent_level: Ebene, von der geerbt wird (``"global"`` oder
            ``"job"``), oder ``None`` auf der obersten Ebene.

    Returns:
        Tupel der drei ``ConfigField``-Definitionen für ``password``,
        ``password_env`` und ``password_file`` auf dieser Ebene.
    """
    return (
        ConfigField(
            key="password",
            resolved_key="password",
            level=level,
            parent_level=parent_level,
            kind="password_choice",
            inheritance="password_choice",
            default=None,
            capability="credentials",
            editor_kind="password",
        ),
        ConfigField(
            key="password_env",
            resolved_key="password_env",
            level=level,
            parent_level=parent_level,
            kind="password_choice",
            inheritance="password_choice",
            default=None,
            capability="credentials",
            editor_kind="text",
        ),
        ConfigField(
            key="password_file",
            resolved_key="password_file",
            level=level,
            parent_level=parent_level,
            kind="password_choice",
            inheritance="password_choice",
            default=None,
            capability="credentials",
            editor_kind="text",
        ),
    )


_CREDENTIAL_FIELDS: tuple[ConfigField, ...] = (
    *_password_choice_fields("job.backup", "global.backup"),
    *_password_choice_fields("backup", "job.backup"),
)


# `backend` ist im Ziel-TOML auswählbar, aktuell aber nur mit dem Wert
# `"restic"` gültig.

_BACKEND_FIELDS: tuple[ConfigField, ...] = (
    ConfigField(
        "backend",
        "backend",
        "backup",
        kind="scalar",
        inheritance="none",
        default="restic",
        capability="backend",
        editor_kind="choice",
    ),
)


# `retention`/`cleanup` sind die Ziel-TOML-Namen. Explizites `False` stoppt
# die Vererbung (scalar_override mit bool).

_EXECUTION_FIELDS: tuple[ConfigField, ...] = (
    _scalar_field(
        "retention",
        "retention",
        "job.backup",
        "global.backup",
        capability="execution",
        default=False,
        kind="scalar",
        editor_kind="bool",
    ),
    _scalar_field(
        "retention",
        "retention",
        "backup",
        "job.backup",
        capability="execution",
        default=False,
        kind="scalar",
        editor_kind="bool",
    ),
    _scalar_field(
        "cleanup",
        "cleanup",
        "job.backup",
        "global.backup",
        capability="execution",
        default=False,
        kind="scalar",
        editor_kind="bool",
    ),
    _scalar_field(
        "cleanup",
        "cleanup",
        "backup",
        "job.backup",
        capability="execution",
        default=False,
        kind="scalar",
        editor_kind="bool",
    ),
    _scalar_field(
        "auto_init",
        "auto_init",
        "job.backup",
        "global.backup",
        capability="execution",
        default=False,
        kind="scalar",
        editor_kind="bool",
    ),
    _scalar_field(
        "auto_init",
        "auto_init",
        "backup",
        "job.backup",
        capability="execution",
        default=False,
        kind="scalar",
        editor_kind="bool",
    ),
)


# `exclude_caches`/`one_file_system` und die `keep_*`-Retention-Felder leben
# im Zielbild unter `global.backup` und werden fachlich auf
# `backend_options.restic.*` bzw. `retention.*` abgebildet.

_RESTIC_BOOL_OPTION_FIELDS: tuple[ConfigField, ...] = tuple(
    field_def
    for raw_name in ("exclude_caches", "one_file_system")
    for field_def in (
        _scalar_field(
            raw_name,
            raw_name,
            "job.backup",
            "global.backup",
            capability="backend_options",
            default=None,
            kind="backend_option",
            backend="restic",
            editor_kind="bool",
        ),
        _scalar_field(
            raw_name,
            raw_name,
            "backup",
            "job.backup",
            capability="backend_options",
            default=None,
            kind="backend_option",
            backend="restic",
            editor_kind="bool",
        ),
    )
)

_RETENTION_SCALAR_NAMES: tuple[str, ...] = (
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
)

_RETENTION_FIELDS: tuple[ConfigField, ...] = tuple(
    field_def
    for raw_name in _RETENTION_SCALAR_NAMES
    for field_def in (
        _scalar_field(
            raw_name,
            raw_name,
            "job.backup",
            "global.backup",
            capability="retention",
            default=None,
            kind="scalar",
            backend="restic",
            editor_kind="text" if raw_name.startswith("keep_within") else "int",
        ),
        _scalar_field(
            raw_name,
            raw_name,
            "backup",
            "job.backup",
            capability="retention",
            default=None,
            kind="scalar",
            backend="restic",
            editor_kind="text" if raw_name.startswith("keep_within") else "int",
        ),
    )
)


# `exclude` und `exclude_files` folgen derselben dreistufigen
# List-Override-Vererbung wie andere Listenfelder.

_JOB_TO_BACKUP_LEVELS: tuple[tuple[Level, Level], ...] = (
    ("job.backup", "global.backup"),
    ("backup", "job.backup"),
)

_FILTER_FIELDS: tuple[ConfigField, ...] = (
    *(
        _list_field(
            "exclude", "exclude", level, parent_level, capability="filters", backend="restic"
        )
        for level, parent_level in _JOB_TO_BACKUP_LEVELS
    ),
    *(
        _list_field(
            "exclude_files",
            "exclude_files",
            level,
            parent_level,
            capability="filters",
            backend="restic",
        )
        for level, parent_level in _JOB_TO_BACKUP_LEVELS
    ),
)


_RESTIC_EXTRA_ARGS_NAMES: tuple[tuple[str, str], ...] = (
    ("extra_restic_backup_args", "extra_backup_args"),
    ("extra_restic_forget_args", "extra_forget_args"),
    ("extra_restic_prune_args", "extra_prune_args"),
)

_RESTIC_EXTRA_ARGS_FIELDS: tuple[ConfigField, ...] = tuple(
    _list_field(
        raw_name,
        resolved_name,
        level,
        parent_level,
        capability="backend_options",
        backend="restic",
    )
    for raw_name, resolved_name in _RESTIC_EXTRA_ARGS_NAMES
    for level, parent_level in _JOB_TO_BACKUP_LEVELS
)


_TIMEOUT_FIELDS: tuple[ConfigField, ...] = (
    _scalar_field(
        "backup_timeout",
        "backup_timeout",
        "job.backup",
        "global.backup",
        capability="timeouts",
        default=None,
        kind="scalar",
        editor_kind="int",
    ),
    _scalar_field(
        "backup_timeout",
        "backup_timeout",
        "backup",
        "job.backup",
        capability="timeouts",
        default=None,
        kind="scalar",
        editor_kind="int",
    ),
    _scalar_field(
        "rclone_timeout",
        "rclone_timeout",
        "job.rclone",
        "global.rclone",
        capability="timeouts",
        default=None,
        kind="scalar",
        editor_kind="int",
    ),
    _scalar_field(
        "rclone_timeout",
        "rclone_timeout",
        "rclone",
        "job.rclone",
        capability="timeouts",
        default=None,
        kind="scalar",
        editor_kind="int",
    ),
    _scalar_field(
        "hook_timeout",
        "hook_timeout",
        "job",
        "global",
        capability="timeouts",
        default=None,
        kind="scalar",
        editor_kind="int",
    ),
    _scalar_field(
        "hook_timeout",
        "hook_timeout",
        "backup",
        "job",
        capability="timeouts",
        default=None,
        kind="scalar",
        editor_kind="int",
    ),
    _scalar_field(
        "hook_timeout",
        "hook_timeout",
        "workflow",
        "job",
        capability="timeouts",
        default=None,
        kind="scalar",
        editor_kind="int",
    ),
    _scalar_field(
        "hook_timeout",
        "hook_timeout",
        "rclone",
        "job",
        capability="timeouts",
        default=None,
        kind="scalar",
        editor_kind="int",
    ),
)


_NOTIFY_TRIGGER_NAMES: tuple[str, ...] = (
    "notify_on_success",
    "notify_on_error",
    "notify_on_skipped",
)

_NOTIFY_DEFAULTS: dict[str, bool] = {
    "notify_on_success": False,
    "notify_on_error": False,
    "notify_on_skipped": False,
}

_NOTIFICATION_LEVELS: tuple[tuple[Level, Level], ...] = (
    ("job", "global"),
    ("backup", "job"),
    ("workflow", "job"),
    ("rclone", "job"),
)

_NOTIFICATION_FIELDS: tuple[ConfigField, ...] = tuple(
    _scalar_field(
        raw_name,
        raw_name,
        level,
        parent_level,
        capability="notifications",
        default=_NOTIFY_DEFAULTS[raw_name],
        kind="scalar",
        editor_kind="bool",
    )
    for raw_name in _NOTIFY_TRIGGER_NAMES
    for level, parent_level in _NOTIFICATION_LEVELS
)


_RCLONE_SCALAR_NAMES: tuple[tuple[str, object, str], ...] = (
    ("transfers", None, "int"),
    ("checkers", None, "int"),
    ("bwlimit", None, "text"),
    ("sync_delete", False, "bool"),
    ("filter_from", None, "text"),
)

_RCLONE_JOB_TO_TASK_LEVELS: tuple[tuple[Level, Level], ...] = (
    ("job.rclone", "global.rclone"),
    ("rclone", "job.rclone"),
)

_RCLONE_OPTION_FIELDS: tuple[ConfigField, ...] = tuple(
    _scalar_field(
        raw_name,
        raw_name,
        level,
        parent_level,
        capability="rclone_options",
        default=default,
        kind="scalar",
        editor_kind=editor_kind,
    )
    for raw_name, default, editor_kind in _RCLONE_SCALAR_NAMES
    for level, parent_level in _RCLONE_JOB_TO_TASK_LEVELS
)

_RCLONE_LIST_OPTION_FIELDS: tuple[ConfigField, ...] = tuple(
    _list_field(raw_name, resolved_name, level, parent_level, capability="rclone_options")
    for raw_name, resolved_name in (
        ("exclude", "exclude"),
        ("extra_rclone_args", "extra_args"),
    )
    for level, parent_level in _RCLONE_JOB_TO_TASK_LEVELS
)


_INPUT_FIELDS: tuple[ConfigField, ...] = (
    _list_field("sources", "sources", "backup", None, capability="input", inheritance="none"),
    _list_field(
        "source_files", "source_files", "backup", None, capability="input", inheritance="none"
    ),
)


_HOOK_LEVELS: tuple[Level, ...] = ("job", "backup", "rclone", "workflow")

_HOOK_FIELDS: tuple[ConfigField, ...] = tuple(
    ConfigField(
        key=hook_name,
        resolved_key=hook_name,
        level=level,
        kind="list",
        inheritance="none",
        default=(),
        capability="hooks",
        editor_kind="list",
    )
    for level in _HOOK_LEVELS
    for hook_name in ("pre_hooks", "post_hooks", "on_error_hooks")
)


_BACKUP_OWN_FIELDS: tuple[ConfigField, ...] = (
    ConfigField(
        key="repository",
        resolved_key="repository",
        level="backup",
        kind="scalar",
        inheritance="none",
        default=None,
        capability="repository",
        editor_kind="text",
    ),
    ConfigField(
        key="schedule",
        resolved_key="schedule",
        level="backup",
        kind="scalar",
        inheritance="none",
        default=None,
        capability="repository",
        editor_kind="cron",
    ),
    ConfigField(
        key="tags",
        resolved_key="tags",
        level="backup",
        kind="list",
        inheritance="none",
        default=(),
        capability="metadata",
        backend="restic",
        editor_kind="list",
    ),
)


_WORKFLOW_OWN_FIELDS: tuple[ConfigField, ...] = (
    ConfigField(
        key="schedule",
        resolved_key="schedule",
        level="workflow",
        kind="scalar",
        inheritance="none",
        default=None,
        capability="workflow",
        editor_kind="cron",
    ),
    ConfigField(
        key="steps",
        resolved_key="steps",
        level="workflow",
        kind="list",
        inheritance="none",
        default=(),
        capability="workflow",
        editor_kind="list",
    ),
)


_RCLONE_OWN_FIELDS: tuple[ConfigField, ...] = (
    ConfigField(
        key="source",
        resolved_key="source",
        level="rclone",
        kind="scalar",
        inheritance="none",
        default=None,
        capability="rclone_task",
        editor_kind="text",
    ),
    ConfigField(
        key="target",
        resolved_key="target",
        level="rclone",
        kind="scalar",
        inheritance="none",
        default=None,
        capability="rclone_task",
        editor_kind="text",
    ),
    ConfigField(
        key="schedule",
        resolved_key="schedule",
        level="rclone",
        kind="scalar",
        inheritance="none",
        default=None,
        capability="rclone_task",
        editor_kind="cron",
    ),
)


_NOTIFICATION_PROVIDER_FIELDS: tuple[ConfigField, ...] = (
    ConfigField(
        key="mail",
        resolved_key="mail",
        level="global.notifications",
        kind="scalar",
        inheritance="none",
        default=None,
        capability="notification_providers",
        editor_kind=None,
    ),
    ConfigField(
        key="pushover",
        resolved_key="pushover",
        level="global.notifications",
        kind="scalar",
        inheritance="none",
        default=None,
        capability="notification_providers",
        editor_kind=None,
    ),
)


#: Vollständiges Domain-Feldschema: ein ``ConfigField`` pro Feld der
#: Resolve-Matrix.
CONFIG_FIELDS: tuple[ConfigField, ...] = (
    *_GLOBAL_FIELDS,
    *_CREDENTIAL_FIELDS,
    *_BACKEND_FIELDS,
    *_EXECUTION_FIELDS,
    *_RESTIC_BOOL_OPTION_FIELDS,
    *_RETENTION_FIELDS,
    *_FILTER_FIELDS,
    *_RESTIC_EXTRA_ARGS_FIELDS,
    *_TIMEOUT_FIELDS,
    *_NOTIFICATION_FIELDS,
    *_RCLONE_OPTION_FIELDS,
    *_RCLONE_LIST_OPTION_FIELDS,
    *_INPUT_FIELDS,
    *_HOOK_FIELDS,
    *_BACKUP_OWN_FIELDS,
    *_WORKFLOW_OWN_FIELDS,
    *_RCLONE_OWN_FIELDS,
    *_NOTIFICATION_PROVIDER_FIELDS,
)


def fields_for_level(level: Level) -> tuple[ConfigField, ...]:
    """Gibt alle Felddefinitionen zurück, deren effektiver Wert auf ``level`` liegt.

    Args:
        level: Die Zielebene (z.B. ``"backup"``, ``"job"``, ``"rclone"``).

    Returns:
        Tupel aller ``ConfigField``-Einträge mit ``field.level == level``, in
        Schema-Reihenfolge.
    """
    return tuple(f for f in CONFIG_FIELDS if f.level == level)


__all__ = [
    "ConfigField",
    "CONFIG_FIELDS",
    "FieldKind",
    "InheritanceKind",
    "Level",
    "fields_for_level",
]
