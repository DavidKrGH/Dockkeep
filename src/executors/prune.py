"""Prune Task Executor."""

from ..models.resolved_config import ResolvedBackupConfig
from ..utils.restic import resolve_env
from .base import BaseExecutor


class PruneExecutor(BaseExecutor):
    """Führt restic prune für einen Job aus.

    Bereinigt unreferenzierte Daten aus dem Repository des Backups
    und gibt Speicherplatz frei.

    Attributes:
        job_name: Name des Jobs.
    """

    def __init__(self, job_name: str, dry_run: bool = False, timeout: int | None = None) -> None:
        """Initialisiert den PruneExecutor.

        Args:
            job_name: Name des Jobs für Logging.
            dry_run: Wenn True, wird --dry-run an restic übergeben.
            timeout: Timeout in Sekunden für den restic-Subprozess. None = kein Timeout.
        """
        super().__init__(job_name, dry_run=dry_run, timeout=timeout)

    async def execute(self, config: ResolvedBackupConfig) -> bool:
        """Führt den Prune-Task aus.

        Args:
            config: Backup-Konfiguration mit repository und Prune-Optionen.

        Returns:
            True bei Erfolg, False bei Fehler.
        """
        env = resolve_env(config.credentials.password, config.credentials.password_file)
        repository = config.repository

        self.logger.info(
            "Starting prune task (repository: %s)%s", repository, self._dry_run_label()
        )
        cmd = self._build_command(repository, config)
        success = await self.run_subprocess(cmd, env=env, stdout_level=self._stdout_log_level())

        if success:
            self.logger.info("Prune completed successfully%s", self._dry_run_label())
        else:
            self.logger.error("Prune failed (%s)", repository)

        return success

    def _build_command(self, repository: str, config: ResolvedBackupConfig) -> list[str]:
        """Konstruiert das restic-prune-Kommando.

        Args:
            repository: Lokaler Pfad oder rclone-Backend des Repositories.
            config: Backup-Konfiguration.

        Returns:
            Kommando als Liste von Strings, bereit für subprocess.
        """
        cmd: list[str] = ["restic", "--repo", repository, "prune"]

        if self.dry_run:
            cmd.append("--dry-run")

        cmd.extend(self._normalize_extra_args(config.backend_options.restic.extra_prune_args))
        return cmd
