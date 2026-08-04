"""Hook execution for job, backup, workflow, and rclone hooks."""

import asyncio
import logging
import os
import shlex
from pathlib import Path

from ..core.stream_logging import ByteTailBuffer, LineLogBuffer
from ..core.subprocesses import stream_command
from ..utils.logging import job_logger_name

HOOK_TAIL_BYTES = 128 * 1024


class HookExecutor:

    def __init__(
        self, job_name: str, scripts_dir: str | Path, hook_timeout: int | None = None
    ) -> None:
        """Initialisiert den HookExecutor.

        Args:
            job_name: Name des Jobs für Logging.
            scripts_dir: Verzeichnis, das als CWD für alle Hooks verwendet wird.
            hook_timeout: Timeout in Sekunden für Hook-Subprozesse. None = kein Timeout.
        """
        self.job_name = job_name
        self.scripts_dir = Path(scripts_dir).expanduser()
        self._resolved_scripts_dir = self.scripts_dir.resolve(strict=False)
        self._hook_timeout = hook_timeout
        self.logger = logging.getLogger(f"{job_logger_name(job_name)}.HookExecutor")

    async def run(self, hooks: list[str]) -> bool:
        """Führt alle Hooks der Liste sequenziell aus.

        Bricht beim ersten Fehler ab – nachfolgende Hooks werden nicht ausgeführt.

        Args:
            hooks: Liste von Hook-Befehlen oder Skript-Pfaden.

        Returns:
            True wenn alle Hooks erfolgreich waren oder die Liste leer ist,
            False beim ersten Fehler.
        """
        for hook in hooks:
            if not await self._run_single(hook):
                return False
        return True

    async def _run_single(self, cmd: str) -> bool:
        """Führt einen einzelnen Hook aus.

        Skript-Erkennung: absoluter Pfad unter ``scripts_dir`` mit optionalen
        Argumenten.
        Inline-Befehle sind nur erlaubt wenn DK_ALLOW_INLINE_HOOKS=true.

        Args:
            cmd: Hook-Befehl oder Skript-Pfad.

        Returns:
            True bei Erfolg, False bei Fehler.
        """
        script_argv, script_error = self._resolve_script_command(cmd)
        if script_error:
            return False

        if script_argv is None:
            if os.environ.get("DK_ALLOW_INLINE_HOOKS", "").lower() != "true":
                self.logger.error(
                    "Inline hook rejected: %r — set DK_ALLOW_INLINE_HOOKS=true"
                    " to allow inline commands",
                    cmd,
                )
                return False
            if not cmd.strip():
                self.logger.error("Inline hook is empty: %r", cmd)
                return False
            args = ["/bin/sh", "-c", cmd]
            hook_label = "inline command"
        else:
            args = script_argv
            hook_label = f"script {shlex.join(script_argv)}"

        if not self._resolved_scripts_dir.exists():
            self.logger.error("Hook scripts dir not found: %s", self._resolved_scripts_dir)
            return False
        if not self._resolved_scripts_dir.is_dir():
            self.logger.error(
                "Hook scripts path is not a directory: %s", self._resolved_scripts_dir
            )
            return False

        self.logger.info("Running hook: %s", hook_label)
        self.logger.debug("Hook command argv: %s", shlex.join(args))

        stdout_logger = LineLogBuffer(lambda line: self.logger.debug("[hook stdout] %s", line))
        stderr_tail = ByteTailBuffer(HOOK_TAIL_BYTES)
        try:
            result = await stream_command(
                args,
                on_stdout=stdout_logger.feed,
                on_stderr=stderr_tail.feed,
                cwd=self.scripts_dir,
                timeout=self._hook_timeout,
            )
        except TimeoutError:
            self._log_hook_stderr(stderr_tail, logging.ERROR)
            self.logger.error("Hook timed out after %ss: %s", self._hook_timeout, " ".join(args))
            return False
        except asyncio.CancelledError:
            self._log_hook_stderr(stderr_tail, logging.ERROR)
            raise
        except FileNotFoundError as exc:
            self.logger.error("Failed to start hook because a path was not found: %s", exc)
            return False
        except OSError as exc:
            self.logger.error("Failed to start hook: %s", exc)
            return False
        finally:
            stdout_logger.flush()

        if result.returncode == 0:
            self._log_hook_stderr(stderr_tail, logging.DEBUG)
            self.logger.info("Hook completed successfully: %s", hook_label)
            return True

        self._log_hook_stderr(stderr_tail, logging.ERROR)
        self.logger.error(
            "Hook failed with exit code %d: %s",
            result.returncode,
            " ".join(args),
        )
        return False

    def _log_hook_stderr(self, stderr_tail: ByteTailBuffer, level: int) -> None:
        for line in stderr_tail.decode().splitlines():
            self.logger.log(level, "[hook stderr] %s", line)

    def _resolve_script_command(self, cmd: str) -> tuple[list[str] | None, bool]:
        """Resolve script hooks safely inside ``scripts_dir``.

        Existing full paths win before tokenization so script paths containing
        whitespace keep working. Otherwise an absolute first shell token is
        treated as a script path and remaining tokens become argv entries.
        """
        stripped = cmd.strip()
        if not stripped:
            return None, False

        full_path = Path(os.path.expanduser(stripped))
        full_resolved = full_path.resolve(strict=False)
        if full_path.is_absolute() and full_resolved.exists():
            script_path, script_error = self._validate_script_path(stripped, full_resolved)
            if script_error:
                return None, True
            return [str(script_path)], False

        try:
            tokens = shlex.split(stripped)
        except ValueError as exc:
            self.logger.error(
                "Invalid hook script command %r: %s. "
                "Check for unmatched quotes or unescaped backslashes.",
                cmd,
                exc,
            )
            return None, True

        if not tokens:
            return None, False

        script_candidate = Path(os.path.expanduser(tokens[0]))
        if not script_candidate.is_absolute():
            return None, False

        resolved = script_candidate.resolve(strict=False)
        script_path, script_error = self._validate_script_path(tokens[0], resolved)
        if script_error:
            return None, True
        return [str(script_path), *tokens[1:]], False

    def _validate_script_path(self, original: str, resolved: Path) -> tuple[Path | None, bool]:
        """Validate a resolved script path and report user-facing errors."""
        if not self._is_inside_scripts_dir(resolved):
            self.logger.error(
                "Hook script rejected outside scripts dir: %s (scripts dir: %s)",
                original,
                self._resolved_scripts_dir,
            )
            return None, True

        if not resolved.exists():
            self.logger.error("Hook script not found: %s", resolved)
            return None, True

        if not resolved.is_file():
            self.logger.error("Hook script is not a file: %s", resolved)
            return None, True

        if not os.access(resolved, os.X_OK):
            self.logger.error(
                "Hook script is not executable: %s (run: chmod +x %s)", resolved, resolved
            )
            return None, True

        return resolved, False

    def _is_inside_scripts_dir(self, path: Path) -> bool:
        return path == self._resolved_scripts_dir or self._resolved_scripts_dir in path.parents
