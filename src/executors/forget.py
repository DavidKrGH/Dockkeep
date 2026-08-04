"""Forget Task Executor."""

from ..models.resolved_config import ResolvedBackupConfig
from ..utils.restic import resolve_env
from .base import BaseExecutor


class ForgetExecutor(BaseExecutor):
    """Führt restic forget für einen Job aus.

    Wendet Retention-Policies auf das Repository des Backups an und
    entfernt Snapshot-Referenzen aus dem Repository-Index.
    """

    def __init__(self, job_name: str, dry_run: bool = False, timeout: int | None = None) -> None:
        """Initialisiert den ForgetExecutor.

        Args:
            job_name: Name des Jobs für Logging.
            dry_run: Wenn True, wird --dry-run an restic übergeben.
            timeout: Timeout in Sekunden für den restic-Subprozess. None = kein Timeout.
        """
        super().__init__(job_name, dry_run=dry_run, timeout=timeout)

    async def execute(self, config: ResolvedBackupConfig) -> bool:
        """Führt den Forget-Task aus.

        Args:
            config: Backup-Konfiguration mit repository, Retention-Policies und Optionen.

        Returns:
            True bei Erfolg, False bei Fehler.
        """
        env = resolve_env(config.credentials.password, config.credentials.password_file)
        repository = config.repository

        self.logger.info(
            "Starting forget task (repository: %s)%s", repository, self._dry_run_label()
        )
        cmd = self._build_command(repository, config)
        success = await self.run_subprocess(cmd, env=env, stdout_level=self._stdout_log_level())

        if success:
            self.logger.info("Forget completed successfully%s", self._dry_run_label())
        else:
            self.logger.error("Forget failed (%s)", repository)

        return success

    def _build_command(self, repository: str, config: ResolvedBackupConfig) -> list[str]:
        """Konstruiert das restic-forget-Kommando.

        Args:
            repository: Lokaler Pfad oder rclone-Backend des Repositories.
            config: Backup-Konfiguration.

        Returns:
            Kommando als Liste von Strings, bereit für subprocess.
        """
        cmd: list[str] = ["restic", "--repo", repository, "forget"]
        retention = config.retention

        if retention.keep_last is not None:
            cmd.extend(["--keep-last", str(retention.keep_last)])

        if retention.keep_hourly is not None:
            cmd.extend(["--keep-hourly", str(retention.keep_hourly)])

        if retention.keep_daily is not None:
            cmd.extend(["--keep-daily", str(retention.keep_daily)])

        if retention.keep_weekly is not None:
            cmd.extend(["--keep-weekly", str(retention.keep_weekly)])

        if retention.keep_monthly is not None:
            cmd.extend(["--keep-monthly", str(retention.keep_monthly)])

        if retention.keep_yearly is not None:
            cmd.extend(["--keep-yearly", str(retention.keep_yearly)])

        if retention.keep_within is not None:
            cmd.extend(["--keep-within", retention.keep_within])

        if retention.keep_within_hourly is not None:
            cmd.extend(["--keep-within-hourly", retention.keep_within_hourly])

        if retention.keep_within_daily is not None:
            cmd.extend(["--keep-within-daily", retention.keep_within_daily])

        if retention.keep_within_weekly is not None:
            cmd.extend(["--keep-within-weekly", retention.keep_within_weekly])

        if retention.keep_within_monthly is not None:
            cmd.extend(["--keep-within-monthly", retention.keep_within_monthly])

        if retention.keep_within_yearly is not None:
            cmd.extend(["--keep-within-yearly", retention.keep_within_yearly])

        if self.dry_run:
            cmd.append("--dry-run")

        cmd.extend(self._normalize_extra_args(config.backend_options.restic.extra_forget_args))
        return cmd
