"""Base Executor Class für alle Task-Executors."""

import asyncio
import logging
import shlex
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from ..core.stream_logging import ByteTailBuffer, LineLogBuffer
from ..core.subprocesses import stream_command
from ..utils.logging import job_logger_name

OPERATIONAL_TAIL_BYTES = 128 * 1024


class BaseExecutor(ABC):
    """Basis-Klasse für alle Task-Executors.

    Stellt gemeinsame Funktionalität bereit: Logging, Subprocess-Ausführung
    und Kommando-Protokollierung. Alle konkreten Executors erben von dieser Klasse.

    Attributes:
        job_name: Name des Jobs, der diesen Executor ausführt.
        logger: Logger-Instanz für den Task.
    """

    def __init__(self, job_name: str, dry_run: bool = False, timeout: int | None = None) -> None:
        """Initialisiert den Executor mit Job-Name und Logger.

        Args:
            job_name: Name des Jobs für Logging und Identifikation.
            dry_run: Wenn True, wird --dry-run an restic/rclone übergeben.
            timeout: Timeout in Sekunden für Subprozesse. None = kein Timeout.
        """
        self.job_name = job_name
        self.dry_run = dry_run
        self._timeout = timeout
        self.logger = logging.getLogger(f"{job_logger_name(job_name)}.{self.__class__.__name__}")

    @abstractmethod
    async def execute(self, config: Any) -> bool:
        """Führt den Task aus.

        Args:
            config: Task-spezifische Konfiguration (konkreter Typ je nach Executor).

        Returns:
            True bei Erfolg, False bei Fehler.
        """

    def log_command(self, command: list[str]) -> None:
        """Loggt das auszuführende Kommando auf DEBUG-Level.

        Args:
            command: Kommando als Liste von Strings (wie für subprocess).
        """
        self.logger.debug("Executing: %s", " ".join(command))

    def _stdout_log_level(self) -> int:
        """Gibt den Log-Level für stdout zurück.

        Returns:
            logging.INFO, unabhängig davon ob ein Dry-Run ausgeführt wird.
        """
        return logging.INFO

    def _dry_run_label(self) -> str:
        return " [DRY RUN]" if self.dry_run else ""

    def _normalize_extra_args(self, extra_args: list[str] | None) -> list[str]:
        """Zerlegt optionale Extraargs shell-ähnlich in argv-Tokens."""
        if extra_args is None:
            return []

        normalized: list[str] = []
        for arg in extra_args:
            normalized.extend(shlex.split(arg))
        return normalized

    async def run_subprocess(
        self,
        command: list[str],
        env: dict[str, str] | None = None,
        stdout_level: int = logging.DEBUG,
        stdout_interceptor: Callable[[str], None] | None = None,
    ) -> bool:
        """Führt ein externes Kommando aus und loggt stdout/stderr.

        stdout wird live zeilenweise auf ``stdout_level`` gestreamt und nie
        vollständig im RAM gehalten. stderr wird in einem begrenzten Tail
        (``OPERATIONAL_TAIL_BYTES``) gesammelt und am Prozessende genau einmal
        geloggt: bei Erfolg (returncode == 0) auf DEBUG, bei Fehler/Timeout/
        Cancellation auf ERROR. Jede stderr-Zeile wird als eigener Log-Record
        geschrieben, damit der GUI-Level-Filter pro Zeile greift.

        Args:
            command: Kommando als Liste (wie für stream_command).
            env: Optionale Umgebungsvariablen. None übernimmt die aktuelle
                Prozessumgebung unverändert.
            stdout_level: Log-Level für stdout-Ausgaben (Default: DEBUG).
            stdout_interceptor: Optionaler Callback für stdout-Zeilen. Wenn
                gesetzt, werden stdout-Zeilen nicht geloggt, sondern an diesen
                Callback übergeben.

        Returns:
            True wenn der Prozess mit Exit-Code 0 beendet wurde, sonst False.
        """
        self.log_command(command)
        stdout_emit = (
            stdout_interceptor
            if stdout_interceptor is not None
            else lambda line: self.logger.log(stdout_level, "[stdout] %s", line)
        )
        stdout_logger = LineLogBuffer(stdout_emit)
        stderr_tail = ByteTailBuffer(OPERATIONAL_TAIL_BYTES)

        try:
            result = await stream_command(
                command,
                on_stdout=stdout_logger.feed,
                on_stderr=stderr_tail.feed,
                env=env,
                timeout=self._timeout,
            )
        except TimeoutError:
            self._log_stderr_tail(stderr_tail, logging.ERROR)
            self.logger.error(
                "Command timed out after %ss: %s",
                self._timeout,
                " ".join(command),
            )
            return False
        except asyncio.CancelledError:
            self._log_stderr_tail(stderr_tail, logging.ERROR)
            raise
        except FileNotFoundError:
            self.logger.error("Command not found: %s", command[0])
            return False
        except OSError as exc:
            self.logger.error("Failed to start process: %s", exc)
            return False
        finally:
            stdout_logger.flush()

        if result.returncode == 0:
            self._log_stderr_tail(stderr_tail, logging.DEBUG)
            return True

        self._log_stderr_tail(stderr_tail, logging.ERROR)
        self.logger.error(
            "Command failed with exit code %d: %s",
            result.returncode,
            " ".join(command),
        )
        return False

    def _log_stderr_tail(self, stderr_tail: ByteTailBuffer, level: int) -> None:
        for line in stderr_tail.decode().splitlines():
            self.logger.log(level, "[stderr] %s", line)
