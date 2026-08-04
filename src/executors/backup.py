"""Backup Task Executor."""

import json
from dataclasses import dataclass
from typing import Any

from ..models.resolved_config import ResolvedBackupConfig
from ..utils.restic import async_ensure_repository, resolve_env
from .base import BaseExecutor


@dataclass(frozen=True)
class BackupSummary:
    """Parsed restic backup summary from ``restic backup --json``."""

    snapshot_id: str
    raw: dict[str, object]
    files_new: int
    files_changed: int
    files_unmodified: int
    dirs_new: int
    dirs_changed: int
    dirs_unmodified: int
    data_added: int
    total_files_processed: int
    total_bytes_processed: int
    total_duration: float


class BackupExecutor(BaseExecutor):
    """Führt Restic-Backup für einen Job aus.

    Attributes:
        job_name: Name des Jobs.
        sources: Quellpfade (werden an restic backup übergeben,
            sofern keine source_files gesetzt sind).
    """

    def __init__(
        self, job_name: str, sources: list[str], dry_run: bool = False, timeout: int | None = None
    ) -> None:
        """Initialisiert den BackupExecutor.

        Args:
            job_name: Name des Jobs für Logging.
            sources: Liste der Quellpfade (config.input.sources, aufgelöst).
            dry_run: Wenn True, wird --dry-run an restic übergeben.
            timeout: Timeout in Sekunden für den restic-Subprozess. None = kein Timeout.
        """
        super().__init__(job_name, dry_run=dry_run, timeout=timeout)
        self.sources = sources
        self.summary: BackupSummary | None = None

    async def execute(self, config: ResolvedBackupConfig) -> bool:
        """Führt den Backup-Task aus.

        Args:
            config: Backup-Konfiguration mit repository, Optionen und Flags.

        Returns:
            True wenn das Backup erfolgreich war, False bei Fehler.
        """
        return await self._backup_to_repo(config.repository, config)

    def _build_command(self, repository: str, config: ResolvedBackupConfig) -> list[str]:
        """Konstruiert das restic-backup-Kommando.

        Args:
            repository: Lokaler Pfad oder rclone-Backend des Repositories.
            config: Backup-Konfiguration.

        Returns:
            Kommando als Liste von Strings, bereit für subprocess.
        """
        cmd: list[str] = ["restic", "--repo", repository, "backup", "--json"]

        for source_file in config.input.source_files:
            cmd.extend(["--files-from", source_file])

        for pattern in config.filters.exclude:
            cmd.append(f"--exclude={pattern}")

        for exclude_file in config.filters.exclude_files:
            cmd.extend(["--exclude-file", exclude_file])

        if config.backend_options.restic.exclude_caches:
            cmd.append("--exclude-caches")

        if config.backend_options.restic.one_file_system:
            cmd.append("--one-file-system")

        for tag in config.tags:
            cmd.extend(["--tag", tag])

        if self.dry_run:
            cmd.append("--dry-run")

        cmd.extend(self._normalize_extra_args(config.backend_options.restic.extra_backup_args))

        if self.sources:
            cmd.extend(["--", *self.sources])

        return cmd

    async def _backup_to_repo(self, repository: str, config: ResolvedBackupConfig) -> bool:
        """Führt ein einzelnes Restic-Backup zu einem Repository durch.

        Args:
            repository: Lokaler Pfad oder rclone-Backend des Repositories.
            config: Backup-Konfiguration.

        Returns:
            True bei Erfolg, False bei Fehler.
        """
        self.logger.info("Starting backup to %s%s", repository, self._dry_run_label())
        self.summary = None

        env = resolve_env(config.credentials.password, config.credentials.password_file)

        if config.execution.auto_init and not await async_ensure_repository(
            repository, env, timeout=self._timeout, log=self.logger
        ):
            self.logger.error("Failed to initialize repository: %s", repository)
            return False

        cmd = self._build_command(repository, config)
        success = await self.run_subprocess(
            cmd,
            env=env,
            stdout_level=self._stdout_log_level(),
            stdout_interceptor=self._capture_json_stdout,
        )

        if success:
            self._log_success_summary()
        else:
            self.logger.error("Backup failed (%s)", repository)

        return success

    def _capture_json_stdout(self, line: str) -> None:
        """Capture restic JSON summary lines without logging raw JSON."""
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            self.logger.debug("Skipping unparsable restic JSON stdout line")
            return
        if not isinstance(payload, dict) or payload.get("message_type") != "summary":
            return
        summary = _summary_from_payload(payload)
        if summary is None:
            self.logger.debug("Skipping incomplete restic backup summary")
            return
        self.summary = summary

    def _log_success_summary(self) -> None:
        if self.summary is None:
            self.logger.info("Backup completed successfully%s", self._dry_run_label())
            return

        self.logger.info(
            (
                "Backup completed%s: snapshot=%s files(new/changed/unmodified)="
                "%d/%d/%d data_added=%dB duration=%.2fs"
            ),
            self._dry_run_label(),
            self.summary.snapshot_id[:12],
            self.summary.files_new,
            self.summary.files_changed,
            self.summary.files_unmodified,
            self.summary.data_added,
            self.summary.total_duration,
        )


def _summary_from_payload(payload: dict[str, Any]) -> BackupSummary | None:
    snapshot_id = payload.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not snapshot_id:
        return None

    return BackupSummary(
        snapshot_id=snapshot_id,
        raw=payload,
        files_new=_int_value(payload, "files_new"),
        files_changed=_int_value(payload, "files_changed"),
        files_unmodified=_int_value(payload, "files_unmodified"),
        dirs_new=_int_value(payload, "dirs_new"),
        dirs_changed=_int_value(payload, "dirs_changed"),
        dirs_unmodified=_int_value(payload, "dirs_unmodified"),
        data_added=_int_value(payload, "data_added"),
        total_files_processed=_int_value(payload, "total_files_processed"),
        total_bytes_processed=_int_value(payload, "total_bytes_processed"),
        total_duration=_float_value(payload, "total_duration"),
    )


def _int_value(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _float_value(payload: dict[str, Any], key: str) -> float:
    value = payload.get(key)
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, int | float):
        return float(value)
    return 0.0
