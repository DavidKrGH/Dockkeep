"""Rclone Sync Task Executor."""

from pathlib import Path

from ..models.resolved_config import ResolvedRcloneSyncTaskConfig
from ..utils.rclone import async_check_remote
from .base import BaseExecutor


class RcloneExecutor(BaseExecutor):
    """Runs configured rclone copy/sync tasks.

    ``sync_delete=True`` uses destructive ``rclone sync`` and is guarded by a
    local-source readability/non-empty check before process startup.
    """

    def __init__(self, job_name: str, dry_run: bool = False, timeout: int | None = None) -> None:
        """Initialisiert den RcloneExecutor.

        Args:
            job_name: Name des Jobs für Logging.
            dry_run: Wenn True, wird --dry-run an rclone übergeben.
            timeout: Timeout in Sekunden für den rclone-Subprozess. None = kein Timeout.
        """
        super().__init__(job_name, dry_run=dry_run, timeout=timeout)

    async def execute(self, config: ResolvedRcloneSyncTaskConfig) -> bool:
        """Führt den Rclone-Sync-Task aus.

        Baut das rclone-Kommando aus der Konfiguration zusammen und führt es aus.

        Args:
            config: Rclone-Sync-Task-Konfiguration.

        Returns:
            True bei Erfolg, False bei Fehler.
        """
        if not await async_check_remote(config.target, timeout=self._timeout):
            self.logger.error("Remote '%s' is not configured in rclone config", config.target)
            return False

        subcommand = "sync" if config.sync_delete else "copy"
        if config.sync_delete and not self._source_is_safe_for_sync(config.source):
            return False

        self.logger.info("Starting rclone %s: %s → %s", subcommand, config.source, config.target)

        cmd = self._build_command(config)
        success = await self.run_subprocess(cmd, stdout_level=self._stdout_log_level())

        if success:
            self.logger.info(
                "Rclone %s completed successfully: %s → %s",
                subcommand,
                config.source,
                config.target,
            )
        else:
            self.logger.error("Rclone %s failed: %s → %s", subcommand, config.source, config.target)

        return success

    def _source_is_safe_for_sync(self, source: str) -> bool:
        source_path = Path(source)
        try:
            if not source_path.is_dir():
                self.logger.error(
                    "Refusing rclone sync_delete for missing or non-directory source: %s",
                    source,
                )
                return False
            if next(source_path.iterdir(), None) is None:
                self.logger.error("Refusing rclone sync_delete for empty source: %s", source)
                return False
        except OSError:
            self.logger.exception("Refusing rclone sync_delete for unreadable source: %s", source)
            return False
        return True

    def _build_command(self, config: ResolvedRcloneSyncTaskConfig) -> list[str]:
        """Konstruiert das rclone-Kommando.

        Wählt rclone copy (sync_delete=False) oder rclone sync (sync_delete=True)
        als Subkommando. Hängt alle konfigurierten Flags an.

        Args:
            config: Rclone-Sync-Task-Konfiguration.

        Returns:
            Kommando als Liste von Strings, bereit für subprocess.
        """
        subcommand = "sync" if config.sync_delete else "copy"
        cmd: list[str] = ["rclone", subcommand, config.source, config.target]

        if config.options.transfers is not None:
            cmd.extend(["--transfers", str(config.options.transfers)])

        if config.options.checkers is not None:
            cmd.extend(["--checkers", str(config.options.checkers)])

        if config.options.bwlimit is not None:
            cmd.extend(["--bwlimit", config.options.bwlimit])

        for pattern in config.exclude:
            cmd.extend(["--exclude", pattern])

        if config.filter_from is not None:
            cmd.extend(["--filter-from", config.filter_from])

        if self.dry_run:
            cmd.append("--dry-run")

        cmd.extend(self._normalize_extra_args(config.options.extra_args))
        return cmd
