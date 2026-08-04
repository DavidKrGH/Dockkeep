"""Resolved (laufzeitfertige) Pydantic-Modelle der Config-Pipeline.

Dieses Modul enthält die ``Resolved*``-Modelle. Sie bilden die laufzeitfertige
Sicht auf die Config nach Anwendung der dreistufigen Vererbung
(``global -> job -> backup/workflow/rclone``):

- vererbbare Listen sind hier immer konkrete Listen (``None`` bedeutet nur
  dort "kein Wert", wo das fachlich ein echter Runtime-Zustand ist),
- vererbbare Skalare sind nach Anwendung der Vererbung aufgelöst,
- nur runtime-relevante Zielmodell-Felder existieren hier,
- backend-spezifische Optionen liegen gekapselt unter
  ``backend_options.restic``.

Diese Modelle werden von ``src.models.resolve.resolve_config()`` erzeugt und
sind reine Datencontainer ohne TOML-Bezug. Sie validieren keine Raw-TOML-
Syntax (das bleibt Aufgabe der ``Raw*``-Modelle in ``src.models.config``).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.models.config import NotificationEventKind


class ResolvedModel(BaseModel):
    """Basis für Resolved-Modelle: strikt, aber ohne TOML-Validatoren."""

    model_config = ConfigDict(extra="forbid")


class ResolvedExecutionConfig(ResolvedModel):
    """Fachliche Ausführungsschalter.

    Attributes:
        retention: Ob nach dem Backup eine Retention-Bereinigung ausgeführt wird.
        cleanup: Ob nach der Retention-Bereinigung ein Cleanup ausgeführt wird.
        auto_init: Ob das Repository bei Bedarf automatisch angelegt wird.
    """

    retention: bool = False
    cleanup: bool = False
    auto_init: bool = False


class ResolvedCredentials(ResolvedModel):
    """Effektive Credential-Daten (Passwort-Choice-Vererbung).

    ``password``, ``password_env`` und ``password_file`` schließen sich
    gegenseitig aus. ``resolve_config()`` löst ``password_env`` zu
    ``password`` auf, wenn die Umgebungsvariable gesetzt ist; ``password_env``
    bleibt als Metadatum erhalten.

    Attributes:
        password: Direktes Passwort, falls gesetzt.
        password_env: Name der Umgebungsvariable, falls gesetzt.
        password_file: Absoluter Pfad zu einer Passwortdatei, falls gesetzt.
    """

    password: str | None = None
    password_env: str | None = None
    password_file: str | None = None


class ResolvedInputConfig(ResolvedModel):
    """Backup-Eingaben: Quellpfade und Quell-Dateilisten.

    Attributes:
        sources: Konkrete Liste von Quellpfaden (kann leer sein).
        source_files: Konkrete Liste von Pfaden zu Dateien mit
            Quellpfad-Listen; jeder Eintrag wird vom Restic-Adapter zu einem
            eigenen ``--files-from``-Flag.
    """

    sources: list[str] = Field(default_factory=list)
    source_files: list[str] = Field(default_factory=list)


class ResolvedFiltersConfig(ResolvedModel):
    """Backup-Filter: Exclude-Patterns und Exclude-Dateien.

    Attributes:
        exclude: Konkrete Liste von Exclude-Patterns (``--exclude``).
        exclude_files: Konkrete Liste von Pfaden zu Exclude-Dateien; jeder
            Eintrag wird vom Restic-Adapter zu einem eigenen
            ``--exclude-file``-Flag.
    """

    exclude: list[str] = Field(default_factory=list)
    exclude_files: list[str] = Field(default_factory=list)


class ResolvedRetentionConfig(ResolvedModel):
    """Restic-Retention-Policy (``restic forget``-Parameter).

    Attributes:
        keep_last: Anzahl der neuesten Snapshots, die behalten werden.
        keep_hourly: Anzahl stündlicher Snapshots.
        keep_daily: Anzahl täglicher Snapshots.
        keep_weekly: Anzahl wöchentlicher Snapshots.
        keep_monthly: Anzahl monatlicher Snapshots.
        keep_yearly: Anzahl jährlicher Snapshots.
        keep_within: Snapshots innerhalb dieser Zeitspanne behalten.
        keep_within_hourly: Stündliche Snapshots innerhalb dieser Zeitspanne behalten.
        keep_within_daily: Tägliche Snapshots innerhalb dieser Zeitspanne behalten.
        keep_within_weekly: Wöchentliche Snapshots innerhalb dieser Zeitspanne behalten.
        keep_within_monthly: Monatliche Snapshots innerhalb dieser Zeitspanne behalten.
        keep_within_yearly: Jährliche Snapshots innerhalb dieser Zeitspanne behalten.
    """

    keep_last: int | None = None
    keep_hourly: int | None = None
    keep_daily: int | None = None
    keep_weekly: int | None = None
    keep_monthly: int | None = None
    keep_yearly: int | None = None
    keep_within: str | None = None
    keep_within_hourly: str | None = None
    keep_within_daily: str | None = None
    keep_within_weekly: str | None = None
    keep_within_monthly: str | None = None
    keep_within_yearly: str | None = None


class ResolvedHooksConfig(ResolvedModel):
    """Hook-Kommandos für Backup oder Workflow.

    Attributes:
        pre_hooks: Vor der Hauptaktion auszuführende Kommandos.
        post_hooks: Nach erfolgreicher Hauptaktion auszuführende Kommandos.
        on_error_hooks: Bei Fehlern auszuführende Kommandos.
    """

    pre_hooks: list[str] = Field(default_factory=list)
    post_hooks: list[str] = Field(default_factory=list)
    on_error_hooks: list[str] = Field(default_factory=list)


class ResolvedNotificationsConfig(ResolvedModel):
    """Effektive Benachrichtigungs-Trigger.

    Attributes:
        notify_on_success: Benachrichtigung bei Erfolg senden.
        notify_on_error: Benachrichtigung bei Fehlern jeder Art senden.
        notify_on_skipped: Benachrichtigung bei Skip senden.
    """

    notify_on_success: bool = False
    notify_on_error: bool = False
    notify_on_skipped: bool = False


class ResolvedMailNotificationConfig(ResolvedModel):
    """Laufzeitfertige SMTP-Provider-Konfiguration."""

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

    @model_validator(mode="after")
    def validate_consistency(self) -> "ResolvedMailNotificationConfig":
        if (self.username_env is None) != (self.password_env is None):
            raise ValueError("username_env and password_env must be configured together")
        return self


class ResolvedPushoverNotificationConfig(ResolvedModel):
    """Laufzeitfertige Pushover-Provider-Konfiguration."""

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

    @field_validator("sound", "device", mode="before")
    @classmethod
    def coerce_empty_string(cls, v: object) -> object:
        if v == "":
            return None
        return v


class ResolvedGlobalNotificationProvidersConfig(ResolvedModel):
    """Globale, effektive Notification-Provider-Konfiguration."""

    report_schedule: str | None = None
    mail: ResolvedMailNotificationConfig | None = None
    pushover: ResolvedPushoverNotificationConfig | None = None


class ResolvedTimeoutsConfig(ResolvedModel):
    """Effektive Subprozess-Timeouts (Sekunden, ``None`` = kein Timeout).

    Attributes:
        backup_timeout: Timeout für Backup-Subprozesse.
        rclone_timeout: Timeout für rclone-Subprozesse.
        hook_timeout: Timeout für Hook-Subprozesse.
    """

    backup_timeout: int | None = None
    rclone_timeout: int | None = None
    hook_timeout: int | None = None


class ResolvedResticBackendOptions(ResolvedModel):
    """Restic-spezifische Backend-Optionen (``backend_options.restic``).

    Attributes:
        exclude_caches: ``--exclude-caches`` setzen.
        one_file_system: ``--one-file-system`` setzen.
        extra_backup_args: Zusätzliche Argumente für ``restic backup``.
        extra_forget_args: Zusätzliche Argumente für ``restic forget``.
        extra_prune_args: Zusätzliche Argumente für ``restic prune``.
    """

    exclude_caches: bool | None = None
    one_file_system: bool | None = None
    extra_backup_args: list[str] = Field(default_factory=list)
    extra_forget_args: list[str] = Field(default_factory=list)
    extra_prune_args: list[str] = Field(default_factory=list)


class ResolvedBackendOptions(ResolvedModel):
    """Backend-spezifische Optionen, gekapselt nach Backend.

    Attributes:
        restic: Restic-spezifische Backend-Optionen.
    """

    restic: ResolvedResticBackendOptions = Field(default_factory=ResolvedResticBackendOptions)


class ResolvedBackupConfig(ResolvedModel):
    """Laufzeitfertige Konfiguration für ein Backup.

    Fasst alle effektiven (bereits vererbten) Werte für ein Backup in
    fachlichen Untergruppen zusammen. Runtime-Code liest bevorzugt diese
    Gruppen statt einzelner Felder.

    Attributes:
        repository: Repository-Pfad (lokaler Pfad oder ``rclone:remote:path``).
        schedule: Cron-Ausdruck für automatische Ausführung, oder ``None``.
        tags: Tags, die dem Snapshot angehängt werden.
        backend: Name des Backup-Backends; aktuell immer ``"restic"``.
        credentials: Effektive Credential-Daten.
        input: Effektive Backup-Eingaben (Quellen, Quell-Dateilisten).
        filters: Effektive Backup-Filter (Exclude-Patterns/-Dateien).
        retention: Effektive Restic-Retention-Policy.
        execution: Effektive Ausführungsschalter (Retention/Cleanup/Auto-Init).
        hooks: Effektive Hook-Kommandos.
        notifications: Effektive Benachrichtigungs-Trigger.
        timeouts: Effektive Subprozess-Timeouts.
        backend_options: Backend-spezifische Optionen, gekapselt nach Backend.
    """

    repository: str
    schedule: str | None = None
    tags: list[str] = Field(default_factory=list)
    backend: Literal["restic"] = "restic"
    credentials: ResolvedCredentials = Field(default_factory=ResolvedCredentials)
    input: ResolvedInputConfig = Field(default_factory=ResolvedInputConfig)
    filters: ResolvedFiltersConfig = Field(default_factory=ResolvedFiltersConfig)
    retention: ResolvedRetentionConfig = Field(default_factory=ResolvedRetentionConfig)
    execution: ResolvedExecutionConfig = Field(default_factory=ResolvedExecutionConfig)
    hooks: ResolvedHooksConfig = Field(default_factory=ResolvedHooksConfig)
    notifications: ResolvedNotificationsConfig = Field(default_factory=ResolvedNotificationsConfig)
    timeouts: ResolvedTimeoutsConfig = Field(default_factory=ResolvedTimeoutsConfig)
    backend_options: ResolvedBackendOptions = Field(default_factory=ResolvedBackendOptions)


class ResolvedWorkflowConfig(ResolvedModel):
    """Laufzeitfertige Konfiguration für einen Workflow.

    Attributes:
        schedule: Cron-Ausdruck für automatische Ausführung, oder ``None``.
        steps: Geordnete Liste von Steps, die sequenziell ausgeführt werden.
        hooks: Effektive Hook-Kommandos.
        notifications: Effektive Benachrichtigungs-Trigger (von Job geerbt).
        timeouts: Effektiver Hook-Timeout (von Job/Global abgeleitet).
    """

    schedule: str | None = None
    steps: list[str] = Field(default_factory=list)
    hooks: ResolvedHooksConfig = Field(default_factory=ResolvedHooksConfig)
    notifications: ResolvedNotificationsConfig = Field(default_factory=ResolvedNotificationsConfig)
    timeouts: ResolvedTimeoutsConfig = Field(default_factory=ResolvedTimeoutsConfig)


class ResolvedRcloneOptionsConfig(ResolvedModel):
    """Effektive Rclone-Optionen für einen Sync-Task.

    Attributes:
        transfers: Anzahl paralleler Transfers.
        checkers: Anzahl paralleler Checker.
        bwlimit: Bandbreitenlimit (z.B. ``"10M"``).
        extra_args: Zusätzliche Argumente für das rclone-Kommando.
    """

    transfers: int | None = None
    checkers: int | None = None
    bwlimit: str | None = None
    extra_args: list[str] = Field(default_factory=list)


class ResolvedRcloneSyncTaskConfig(ResolvedModel):
    """Laufzeitfertige Konfiguration für einen Rclone-Sync-Task.

    Attributes:
        source: Quellpfad (lokal), typischerweise das lokale Repository.
        target: Ziel-Pfad auf dem Rclone-Remote.
        sync_delete: Dateien im Ziel löschen, die in der Quelle fehlen.
        exclude: Liste von Exclude-Patterns.
        filter_from: Pfad zu einer Rclone-Filter-Datei.
        schedule: Cron-Ausdruck für automatische Ausführung, oder ``None``.
        options: Effektive Rclone-Optionen (Transfers, Checkers, Bwlimit,
            Extra-Args).
        notifications: Effektive Benachrichtigungs-Trigger.
        timeouts: Effektiver rclone-Timeout.
    """

    source: str
    target: str
    sync_delete: bool | None = None
    exclude: list[str] = Field(default_factory=list)
    filter_from: str | None = None
    schedule: str | None = None
    hooks: ResolvedHooksConfig = Field(default_factory=ResolvedHooksConfig)
    options: ResolvedRcloneOptionsConfig = Field(default_factory=ResolvedRcloneOptionsConfig)
    notifications: ResolvedNotificationsConfig = Field(default_factory=ResolvedNotificationsConfig)
    timeouts: ResolvedTimeoutsConfig = Field(default_factory=ResolvedTimeoutsConfig)


class ResolvedJobConfig(ResolvedModel):
    """Laufzeitfertige Konfiguration für einen Job.

    Hält die bereits auf Job-Ebene aufgelösten Werte (vor Vererbung an
    Backups/Workflows/Rclone-Tasks) sowie die resultierenden
    Backup-/Workflow-/Rclone-Task-Configs.

    Attributes:
        backup: Resolved-Configs aller Backups dieses Jobs (Key = Backup-Name).
        rclone: Resolved-Configs aller Rclone-Sync-Tasks (Key = Task-Name).
        workflows: Resolved-Configs aller Workflows (Key = Workflow-Name).
    """

    backup: dict[str, ResolvedBackupConfig] = Field(default_factory=dict)
    rclone: dict[str, ResolvedRcloneSyncTaskConfig] = Field(default_factory=dict)
    workflows: dict[str, ResolvedWorkflowConfig] = Field(default_factory=dict)
    hooks: ResolvedHooksConfig = Field(default_factory=ResolvedHooksConfig)
    notifications: ResolvedNotificationsConfig = Field(default_factory=ResolvedNotificationsConfig)
    timeouts: ResolvedTimeoutsConfig = Field(default_factory=ResolvedTimeoutsConfig)


class ResolvedGlobalConfig(ResolvedModel):
    """Globale, nicht-vererbte Resolved-Einstellungen.

    Attributes:
        log_level: Log-Level für alle Jobs.
        log_retention_days: Anzahl Tage, für die Logs aufbewahrt werden.
        notifications: Globale Provider-Konfiguration.
    """

    log_level: str = "debug"
    log_retention_days: int | None = None
    lock_retry_count: int | None = None
    lock_retry_delay: int | None = None
    notifications: ResolvedGlobalNotificationProvidersConfig = Field(
        default_factory=ResolvedGlobalNotificationProvidersConfig
    )


class ResolvedAppConfig(ResolvedModel):
    """Laufzeitfertige Gesamt-Konfiguration.

    Attributes:
        global_: Globale, nicht-vererbte Einstellungen.
        jobs: Resolved-Configs aller Jobs (Key = Job-Name).
    """

    global_: ResolvedGlobalConfig = Field(default_factory=ResolvedGlobalConfig)
    jobs: dict[str, ResolvedJobConfig] = Field(default_factory=dict)
