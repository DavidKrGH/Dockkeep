"""CLI entry point for Dockkeep.

Einstiegspunkt für alle Kommandos:
  python -m src.main run <job>.backup.<name>|<job>.workflow.<name>
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
import tomllib
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from .core.job_runner import JobRunner
from .models.resolved_config import (
    ResolvedAppConfig,
    ResolvedBackupConfig,
    ResolvedJobConfig,
    ResolvedRcloneSyncTaskConfig,
)
from .notifications.context import build_notification_context
from .scheduler.cron import CronScheduler, SchedulerStartState
from .scheduler.status import SchedulerStatusReader, SchedulerStatusWriter
from .services.errors import NotFoundServiceError, ServiceError
from .services.logs import resolve_job_log_dir, tail_file_lines
from .services.run_control import RunControlClient, default_socket_path
from .services.run_manager import (
    RunKind,
    RunManager,
    RunOrigin,
    RunStatus,
    format_run_target,
)
from .services.scheduling import next_run_datetime
from .services.terminal_hooks import terminal_notification_only_hook
from .utils.logging import log_base_dir, setup_system_logger
from .utils.restic import resolve_env
from .utils.targets import parse_task_selector
from .utils.validation import collect_config_warnings, load_config

DEFAULT_CONFIG_PATH = Path(os.environ.get("DK_CONFIG_DIR", "/config")) / "config.toml"
LOG_BASE_DIR: Path | None = None

EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_CONFIG_ERROR = 2
EXIT_LOCK_ERROR = 3
EXIT_CANCELLED = 130


def _effective_log_base_dir() -> Path:
    return LOG_BASE_DIR if LOG_BASE_DIR is not None else log_base_dir()


def _resolve_dk_mode() -> tuple[Literal["gui", "cli"], str | None]:
    """Resolve DK_MODE to the effective runtime mode.

    Returns:
        Tuple of effective mode and invalid raw value, if one was provided.
    """
    raw = os.environ.get("DK_MODE")
    if raw is None or raw == "" or raw == "gui":
        return "gui", None
    if raw == "cli":
        return "cli", None
    return "gui", raw


def _log_invalid_dk_mode(raw: str) -> None:
    """Log invalid DK_MODE values and document GUI fallback."""
    logger = setup_system_logger()
    logger.warning("Invalid DK_MODE=%r; falling back to gui mode", raw)


def _config_log_level(config: ResolvedAppConfig) -> str:
    level = getattr(config.global_, "log_level", "debug")
    return level if isinstance(level, str) else "debug"


_CLI_MODE_EPILOG = """\
Common commands
  dk run JOB.backup.NAME          Run a configured backup
  dk run --dry-run JOB.rclone.N   Preview a configured rclone sync
  dk jobs list                    Show configured jobs
  dk jobs tasks JOB               Show backups and rclone tasks for a job
  dk logs show JOB --tail 100     Show the last 100 log lines for a job
  dk shell JOB.backup.NAME        Open a shell with backend env vars set

Mode
  DK_MODE=cli enables the full command palette shown above. With DK_MODE=gui
  (or unset), Dockkeep is operated through the web UI and only 'dk shell' and
  'dk config validate' remain available from the CLI.

Prerequisites
  Restic:  set auto_init = true in config.toml, or run once: restic init --repo <path>
  Rclone:  configure remotes once with: rclone config
  Password: set one of password / password_env / password_file per level;
            password_env names the env var to read (no default name)

Exit codes
  0  success    1  error       2  config error
  3  lock error  130  Ctrl+C
"""

_GUI_MODE_EPILOG = """\
Dockkeep runs in GUI mode (DK_MODE=gui or unset). Use the web UI to run,
schedule, and inspect jobs. Only these commands are available from the CLI:

  dk shell [JOB[.backup.NAME|.rclone.NAME]]   Backend shell for native
                                              restic/rclone maintenance
  dk config validate                          Validate the config file

All other commands (run, jobs, schedule, scheduler, runs, logs) belong to the
web UI in this mode; set DK_MODE=cli for the direct CLI/scheduler mode.

Exit codes
  0  success    1  error       2  config error
"""

_GUI_MODE_ALLOWED_COMMANDS = {"shell", "config-validate"}
_GUI_MODE_BLOCKED_GROUPS = {"run", "jobs", "schedule", "scheduler", "runs", "logs"}

_GUI_MODE_BLOCKED_MESSAGE = (
    "✗ Dockkeep runs in GUI mode. Use the web UI for this command; only "
    "'dk shell' and 'dk config validate' are available from the CLI "
    "(set DK_MODE=cli for the full command palette)."
)


def _first_command_token(argv: list[str]) -> str | None:
    """Return the first positional command token without fully parsing args.

    The GUI-mode guard must run before argparse handles nested subcommands and
    ``--help`` for blocked groups; otherwise argparse exits before Dockkeep can
    print the mode-specific message.
    """
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in {"-h", "--help"}:
            return None
        if token == "--config":
            if index + 1 >= len(argv):
                return None
            index += 2
            continue
        if token.startswith("--config="):
            index += 1
            continue
        if token.startswith("-"):
            return None
        return token
    return None


def _build_parser(mode: Literal["gui", "cli"] = "cli") -> argparse.ArgumentParser:
    """Erstellt den CLI-Argument-Parser mit allen Subcommands.

    Args:
        mode: Effektiver Betriebsmodus. Im GUI-Modus werden blockierte Commands
            aus der Help-Ausgabe ausgeblendet, bleiben aber als Parser-Choices
            registriert, damit der Modus-Guard in :func:`main` eine klare
            Hinweismeldung statt eines argparse "invalid choice"-Fehlers liefert.

    Returns:
        Konfigurierter ArgumentParser.
    """
    gui_mode = mode == "gui"

    parser = argparse.ArgumentParser(
        prog="dk",
        description="Run and inspect configured Restic/Rclone backup jobs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_GUI_MODE_EPILOG if gui_mode else _CLI_MODE_EPILOG,
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        metavar="PATH",
        help=f"Path to TOML config file (default: {DEFAULT_CONFIG_PATH})",
    )

    subparsers = parser.add_subparsers(dest="command_group", metavar="COMMAND")
    subparsers.required = False

    def _add_mode_gated_group(name: str, help_text: str) -> argparse.ArgumentParser:
        """Registriert einen GUI-blockierten Command; ohne ``help`` bleibt er
        im GUI-Modus aus der Help-Liste ausgeblendet, aber parsebar."""
        if gui_mode:
            return subparsers.add_parser(name)
        return subparsers.add_parser(name, help=help_text)

    run_parser = _add_mode_gated_group(
        "run", "Execute a configured backup, workflow, rclone task, or backup substep"
    )
    run_parser.set_defaults(command="run")
    run_parser.add_argument(
        "task_selector",
        metavar="TASK_SELECTOR",
        nargs="?",
        default=None,
        help=(
            "Target: JOB.backup.NAME, JOB.workflow.NAME, JOB.rclone.NAME, "
            "or JOB.backup.NAME.<backup|retention|cleanup>"
        ),
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview restic/rclone operations; auto_init may still initialize a missing repo",
    )

    config_parser = subparsers.add_parser("config", help="Validate configuration")
    config_subparsers = config_parser.add_subparsers(dest="config_command", metavar="COMMAND")
    config_subparsers.required = True
    config_validate_parser = config_subparsers.add_parser(
        "validate",
        help="Validate the config file and exit (exit code 2 on error)",
    )
    config_validate_parser.set_defaults(command="config-validate")

    jobs_parser = _add_mode_gated_group("jobs", "List jobs, tasks, and workflows")
    jobs_subparsers = jobs_parser.add_subparsers(dest="jobs_command", metavar="COMMAND")
    jobs_subparsers.required = True
    jobs_list_parser = jobs_subparsers.add_parser("list", help="List all configured jobs")
    jobs_list_parser.set_defaults(command="jobs-list")

    jobs_tasks_parser = jobs_subparsers.add_parser(
        "tasks",
        help="List backups and rclone tasks for a job",
    )
    jobs_tasks_parser.set_defaults(command="jobs-tasks")
    jobs_tasks_parser.add_argument(
        "job_name",
        metavar="JOB",
        nargs="?",
        default=None,
        help="Job name; omit to show available jobs",
    )

    jobs_workflows_parser = jobs_subparsers.add_parser(
        "workflows",
        help="List workflows for a job",
    )
    jobs_workflows_parser.set_defaults(command="jobs-workflows")
    jobs_workflows_parser.add_argument(
        "job_name",
        metavar="JOB",
        nargs="?",
        default=None,
        help="Job name; omit to show available jobs",
    )

    schedule_parser = _add_mode_gated_group("schedule", "Show upcoming scheduled runs")
    schedule_subparsers = schedule_parser.add_subparsers(
        dest="schedule_command",
        metavar="COMMAND",
    )
    schedule_subparsers.required = True
    schedule_next_parser = schedule_subparsers.add_parser(
        "next",
        help="Show upcoming scheduled runs for all jobs or a specific job",
    )
    schedule_next_parser.set_defaults(command="schedule-next")
    schedule_next_parser.add_argument(
        "job_name",
        metavar="JOB",
        nargs="?",
        default=None,
        help="Job name (optional, shows all jobs if omitted)",
    )

    scheduler_parser = _add_mode_gated_group("scheduler", "Inspect scheduler state")
    scheduler_subparsers = scheduler_parser.add_subparsers(
        dest="scheduler_command",
        metavar="COMMAND",
    )
    scheduler_subparsers.required = True
    scheduler_status_parser = scheduler_subparsers.add_parser(
        "status",
        help="Show scheduler status (running/stopped/unknown/config_error/scheduler_lock_error)",
    )
    scheduler_status_parser.set_defaults(command="scheduler_status")

    runs_parser = _add_mode_gated_group("runs", "List or cancel scheduler-owned runs")
    runs_subparsers = runs_parser.add_subparsers(dest="runs_command", metavar="COMMAND")
    runs_subparsers.required = True
    runs_list_parser = runs_subparsers.add_parser(
        "list",
        help="List active scheduler runs via control socket (exit code 1 if no scheduler running)",
    )
    runs_list_parser.set_defaults(command="runs-list")

    runs_cancel_parser = runs_subparsers.add_parser(
        "cancel",
        help="Cancel a scheduler run by its run id (exit code 1 if no scheduler running)",
    )
    runs_cancel_parser.set_defaults(command="runs-cancel")
    runs_cancel_parser.add_argument(
        "run_id",
        metavar="RUN_ID",
        help="Run id as shown by runs list",
    )

    logs_parser = _add_mode_gated_group("logs", "Show or follow job logs")
    logs_subparsers = logs_parser.add_subparsers(dest="logs_command", metavar="COMMAND")
    logs_subparsers.required = True
    logs_show_parser = logs_subparsers.add_parser(
        "show",
        help="Show logs for a job",
    )
    logs_show_parser.set_defaults(command="logs-show")
    logs_show_parser.add_argument("job_name", metavar="JOB", help="Name of the job")
    logs_show_parser.add_argument(
        "--days",
        type=int,
        default=1,
        metavar="N",
        help="Number of days to show logs for (default: 1 = today only)",
    )
    logs_show_parser.add_argument(
        "--tail",
        type=int,
        default=None,
        metavar="N",
        help="Show only the last N lines",
    )

    logs_tail_parser = logs_subparsers.add_parser(
        "tail",
        help="Follow logs in real time (like tail -f); Ctrl+C to stop",
    )
    logs_tail_parser.set_defaults(command="logs-tail")
    logs_tail_parser.add_argument("job_name", metavar="JOB", help="Name of the job")

    shell_parser = subparsers.add_parser("shell", help="Open a backend shell for a job target")
    shell_parser.add_argument(
        "task_selector",
        metavar="JOB[.TASK]",
        nargs="?",
        default=None,
        help=(
            "Job name to list tasks, or JOB.backup.<name> / "
            "JOB.rclone.<name> to open a shell directly"
        ),
    )
    shell_parser.set_defaults(command="shell")

    return parser


_FOREGROUND_RUN_EXIT_CODES: dict[RunStatus, int] = {
    RunStatus.SUCCESS: EXIT_SUCCESS,
    RunStatus.FAILED: EXIT_ERROR,
    RunStatus.UNEXPECTED_ERROR: EXIT_ERROR,
    RunStatus.CONFIG_ERROR: EXIT_CONFIG_ERROR,
    RunStatus.LOCK_ERROR: EXIT_LOCK_ERROR,
    RunStatus.CANCELLED: EXIT_CANCELLED,
}


def _foreground_run_exit_code(status: RunStatus) -> int:
    """Map a terminal foreground-run status to a process exit code.

    Non-terminal statuses (``QUEUED``/``RUNNING``) and the scheduler-only
    ``SKIPPED`` status are not expected for foreground runs and map defensively
    to ``EXIT_ERROR``.

    Args:
        status: The terminal status of the ``dk run`` foreground run.

    Returns:
        The corresponding process exit code.
    """
    return _FOREGROUND_RUN_EXIT_CODES.get(status, EXIT_ERROR)


def _print_run_targets(config: ResolvedAppConfig) -> None:
    """Print available run targets when ``dk run`` is called without a target."""
    if not config.jobs:
        print("No jobs configured.")
        return

    print("Available run targets:\n")
    for job_name in sorted(config.jobs):
        job = config.jobs[job_name]
        print(f"{job_name}:")
        for backup_name in sorted(job.backup):
            print(f"  dk run {job_name}.backup.{backup_name}")
            print(f"  dk run {job_name}.backup.{backup_name}.backup")
            print(f"  dk run {job_name}.backup.{backup_name}.retention")
            print(f"  dk run {job_name}.backup.{backup_name}.cleanup")
        for rclone_name in sorted(job.rclone):
            print(f"  dk run {job_name}.rclone.{rclone_name}")
        for workflow_name in sorted(job.workflows):
            print(f"  dk run {job_name}.workflow.{workflow_name}")
        print()


def cmd_run_job(args: argparse.Namespace, config_path: Path) -> int:
    """Runs one CLI-mode foreground target without AppData persistence.

    Wird nur im CLI-Modus (``DK_MODE=cli``) erreicht; der Modus-Guard in
    :func:`main` blockiert den Handler im GUI-Modus. Lädt die Config, ermittelt
    ob das Ziel ein Backup, Rclone-Task, Workflow oder Backup-Substep ist.
    ``Ctrl+C`` (SIGINT) bricht den aktiven Lauf kontrolliert ab.
    """
    logger = setup_system_logger()

    try:
        config = load_config(config_path)
    except FileNotFoundError:
        logger.error("Config file not found: %s", config_path)
        return EXIT_CONFIG_ERROR
    except tomllib.TOMLDecodeError as exc:
        logger.error("Invalid TOML in config file: %s", exc)
        return EXIT_CONFIG_ERROR
    except ValidationError as exc:
        logger.error("Config validation failed:\n%s", exc)
        return EXIT_CONFIG_ERROR
    except ValueError as exc:
        logger.error("Config error: %s", exc)
        return EXIT_CONFIG_ERROR
    logger = setup_system_logger(_config_log_level(config))

    task_selector: str | None = getattr(args, "task_selector", None)
    if task_selector is None:
        _print_run_targets(config)
        return EXIT_SUCCESS

    try:
        parsed = parse_task_selector(task_selector)
    except ValueError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG_ERROR

    job_name = parsed.job
    job_config = config.jobs.get(job_name)
    if job_config is None:
        logger.error("Job '%s' not found in config", job_name)
        return EXIT_CONFIG_ERROR

    is_workflow = parsed.kind == "workflow" and parsed.name in job_config.workflows
    is_backup = parsed.kind == "backup" and parsed.name in job_config.backup
    is_rclone = parsed.kind == "rclone" and parsed.name in job_config.rclone
    is_backup_substep = parsed.kind == "backup_step" and parsed.name in job_config.backup

    if not is_workflow and not is_backup and not is_rclone and not is_backup_substep:
        logger.error(
            "Unknown task '%s' for job '%s'. "
            "Valid backups: %s. Valid rclone tasks: %s. Defined workflows: %s",
            task_selector,
            job_name,
            ", ".join(sorted(job_config.backup)) or "(none)",
            ", ".join(sorted(job_config.rclone)) or "(none)",
            ", ".join(sorted(job_config.workflows)) or "(none)",
        )
        return EXIT_CONFIG_ERROR

    dry_run: bool = getattr(args, "dry_run", False)
    if dry_run:
        logger.info("[DRY RUN] %s — no data will be written or deleted", task_selector)

    async def operation(mark_not_cancellable: Callable[[], None]) -> bool:
        runner = JobRunner(
            job_name,
            job_config,
            log_level=config.global_.log_level,
            dry_run=dry_run,
            on_operational_complete=mark_not_cancellable,
            lock_retry_count=config.global_.lock_retry_count,
            lock_retry_delay=config.global_.lock_retry_delay,
        )
        if is_workflow:
            return await runner.run_workflow(parsed.name)
        if is_backup:
            return await runner.run_backup(parsed.name)
        if is_rclone:
            return await runner.run_step(f"rclone.{parsed.name}")
        if is_backup_substep:
            return await runner.run_step(f"backup.{parsed.name}.{parsed.substep}")
        raise AssertionError(f"Unhandled task kind: {parsed.kind!r}")

    async def _run_foreground() -> int:
        manager = RunManager(on_terminal=terminal_notification_only_hook())
        record = await manager.start(
            RunOrigin.MANUAL,
            job_name,
            parsed.kind if parsed.kind != "backup_step" else "backup",
            f"{parsed.name}.{parsed.substep}" if is_backup_substep else parsed.name,
            operation,
            dry_run=dry_run,
            notify_ctx=build_notification_context(
                providers=config.global_.notifications,
                notifications=(
                    job_config.workflows[parsed.name].notifications
                    if is_workflow
                    else (
                        job_config.rclone[parsed.name].notifications
                        if is_rclone
                        else job_config.backup[parsed.name].notifications
                    )
                ),
                logger=logging.getLogger(f"dockkeep.jobs.{job_name}"),
                task_type=("workflow" if is_workflow else "rclone" if is_rclone else "backup"),
                task_name=parsed.name,
                display_target=task_selector,
                log_path=_effective_log_base_dir() / job_name / f"{date.today()}.log",
            ),
        )

        loop = asyncio.get_running_loop()
        pending: set[asyncio.Task[None]] = set()

        async def _cancel_after_sigint() -> None:
            with suppress(NotFoundServiceError):
                await manager.cancel(record.run_id)

        def _consume_cancel_result(task: asyncio.Task[None]) -> None:
            pending.discard(task)
            with suppress(asyncio.CancelledError):
                task.result()

        def _on_sigint() -> None:
            task = loop.create_task(_cancel_after_sigint())
            pending.add(task)
            task.add_done_callback(_consume_cancel_result)

        try:
            loop.add_signal_handler(signal.SIGINT, _on_sigint)
        except (NotImplementedError, RuntimeError):
            sigint_installed = False
        else:
            sigint_installed = True

        try:
            final = await manager.join(record.run_id)
        finally:
            if sigint_installed:
                loop.remove_signal_handler(signal.SIGINT)

        if final.status == RunStatus.CANCELLED:
            logger.info("Run cancelled: %s", task_selector)
        elif final.error:
            logger.error("%s", final.error)
        return _foreground_run_exit_code(final.status)

    try:
        return asyncio.run(_run_foreground())
    except KeyboardInterrupt:
        return EXIT_CANCELLED


def cmd_validate_config(config_path: Path) -> int:
    """Validates the config file and prints a human-readable summary.

    Args:
        config_path: Pfad zur TOML-Konfigurationsdatei.

    Returns:
        Exit-Code: 0 valide, 2 Config-Fehler.
    """
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        print("✗ Config validation failed\n")
        print(f"Config file not found: {config_path}")
        return EXIT_CONFIG_ERROR
    except tomllib.TOMLDecodeError as exc:
        print("✗ Config validation failed\n")
        print(f"Invalid TOML: {exc}")
        return EXIT_CONFIG_ERROR
    except ValidationError as exc:
        print("✗ Config validation failed\n")
        errors_by_job: dict[str, list[str]] = {}
        other_errors: list[str] = []
        for err in exc.errors():
            loc = err["loc"]
            msg = err["msg"]
            if len(loc) >= 2 and loc[0] == "jobs":
                job_key = str(loc[1])
                field = ".".join(str(p) for p in loc[2:]) if len(loc) > 2 else ""
                errors_by_job.setdefault(job_key, []).append(f"{field}: {msg}" if field else msg)
            else:
                other_errors.append(".".join(str(p) for p in loc) + ": " + msg)
        for job_key, msgs in errors_by_job.items():
            print(f"Job '{job_key}':")
            for m in msgs:
                print(f"  - {m}")
        for m in other_errors:
            print(f"  - {m}")
        print("\nPlease fix these errors and try again.")
        return EXIT_CONFIG_ERROR
    except ValueError as exc:
        print("✗ Config validation failed\n")
        print(str(exc))
        print("\nPlease fix these errors and try again.")
        return EXIT_CONFIG_ERROR

    job_count = len(config.jobs)
    backup_count = sum(len(job.backup) for job in config.jobs.values())
    workflow_count = sum(len(job.workflows) for job in config.jobs.values())
    print("✓ Config validation successful")
    print(f"✓ Found {job_count} job{'s' if job_count != 1 else ''}")
    print(
        f"✓ Found {backup_count} backup{'s' if backup_count != 1 else ''} "
        f"across {job_count} job{'s' if job_count != 1 else ''}"
    )
    print(f"✓ Found {workflow_count} workflow{'s' if workflow_count != 1 else ''}")
    warnings = collect_config_warnings(config)
    for warning in warnings:
        print(f"⚠ {warning}")
    if workflow_count:
        print("✓ All cron expressions valid")
    return EXIT_SUCCESS


def cmd_list_jobs(args: argparse.Namespace, config_path: Path) -> int:
    """Listet alle konfigurierten Jobs auf.

    Args:
        args: Geparste CLI-Argumente.
        config_path: Pfad zur TOML-Konfigurationsdatei.

    Returns:
        Exit-Code: 0 Erfolg, 2 Config-Fehler.
    """
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        print(f"✗ Config file not found: {config_path}")
        return EXIT_CONFIG_ERROR
    except (tomllib.TOMLDecodeError, ValidationError, ValueError) as exc:
        print(f"✗ Config error: {exc}")
        return EXIT_CONFIG_ERROR

    jobs = config.jobs

    if not jobs:
        print("No jobs configured.")
        return EXIT_SUCCESS

    print("Jobs:")
    for name, job in jobs.items():
        print(f"  {name}")
        if job.backup:
            backup_parts = [
                f"{backup_name} ({backup.repository})" for backup_name, backup in job.backup.items()
            ]
            print(f"    Backups: {', '.join(backup_parts)}")
        else:
            print("    Backups: (none)")
        if job.rclone:
            rclone_parts = [
                f"{rclone_name} ({task.source} → {task.target})"
                for rclone_name, task in job.rclone.items()
            ]
            print(f"    Rclone tasks: {', '.join(rclone_parts)}")
        else:
            print("    Rclone tasks: (none)")
        if job.workflows:
            workflow_parts = [
                f"{wf_name} ({wf.schedule or 'manual'})" for wf_name, wf in job.workflows.items()
            ]
            print(f"    Workflows: {', '.join(workflow_parts)}")
        else:
            print("    Workflows: (none)")
        print()

    return EXIT_SUCCESS


def _print_available_jobs(config: ResolvedAppConfig) -> None:
    """Print configured job names for commands that need a job argument."""
    if not config.jobs:
        print("No jobs configured.")
        return

    print("Available jobs:")
    for job_name in sorted(config.jobs):
        print(f"  {job_name}")


def _print_missing_job_help(command: str, config: ResolvedAppConfig) -> None:
    """Print contextual help when a job-scoped command was called without JOB."""
    print(f"Missing JOB. Usage: dk {command} JOB\n")
    _print_available_jobs(config)


def _next_run_display(schedule: str) -> str:
    next_run = next_run_datetime(schedule)
    return next_run.strftime("%Y-%m-%d %H:%M") if next_run is not None else "invalid schedule"


def _next_run_or_error(schedule: str, target: str) -> datetime | None:
    next_run = next_run_datetime(schedule)
    if next_run is None:
        print(f"✗ Invalid schedule for {target}: {schedule!r}")
    return next_run


def cmd_list_workflows(args: argparse.Namespace, config_path: Path) -> int:
    """Listet alle Workflows eines Jobs auf.

    Args:
        args: Geparste CLI-Argumente (enthält args.job_name).
        config_path: Pfad zur TOML-Konfigurationsdatei.

    Returns:
        Exit-Code: 0 Erfolg, 2 Config-Fehler.
    """
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        print(f"✗ Config file not found: {config_path}")
        return EXIT_CONFIG_ERROR
    except (tomllib.TOMLDecodeError, ValidationError, ValueError) as exc:
        print(f"✗ Config error: {exc}")
        return EXIT_CONFIG_ERROR

    job_name = getattr(args, "job_name", None)
    if job_name is None:
        _print_missing_job_help("jobs workflows", config)
        return EXIT_SUCCESS

    job_config = config.jobs.get(job_name)
    if job_config is None:
        print(f"✗ Job '{job_name}' not found in config")
        return EXIT_CONFIG_ERROR

    if not job_config.workflows:
        print(f"No workflows defined for job '{job_name}'.")
        return EXIT_SUCCESS

    print(f"Workflows for job '{job_name}':\n")
    for wf_name, wf in job_config.workflows.items():
        if wf.schedule:
            status = "scheduled"
            next_run_str = _next_run_display(wf.schedule)
        else:
            status = "manual"
            next_run_str = "manual only"
        steps_str = " → ".join(wf.steps)
        print(f"  {wf_name} ({status})")
        print(f"    Schedule: {wf.schedule or '–'}")
        print(f"    Steps:    {steps_str}")
        print(f"    Next run: {next_run_str}")
        print()

    return EXIT_SUCCESS


def cmd_list_tasks(args: argparse.Namespace, config_path: Path) -> int:
    """Lists all backups and rclone tasks for a job.

    Args:
        args: Parsed CLI arguments (contains args.job_name).
        config_path: Path to TOML config file.

    Returns:
        Exit code: 0 success, 2 config error.
    """
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        print(f"✗ Config file not found: {config_path}")
        return EXIT_CONFIG_ERROR
    except (tomllib.TOMLDecodeError, ValidationError, ValueError) as exc:
        print(f"✗ Config error: {exc}")
        return EXIT_CONFIG_ERROR

    job_name = getattr(args, "job_name", None)
    if job_name is None:
        _print_missing_job_help("jobs tasks", config)
        return EXIT_SUCCESS

    job_config = config.jobs.get(job_name)
    if job_config is None:
        print(f"✗ Job '{job_name}' not found in config")
        return EXIT_CONFIG_ERROR

    print(f"Tasks for job '{job_name}':\n")
    if job_config.backup:
        print("  Backups:")
        for backup_name, backup in job_config.backup.items():
            if backup.schedule:
                next_run_str = _next_run_display(backup.schedule)
            else:
                next_run_str = "manual only"
            print(f"  {backup_name}")
            print(f"    Repository: {backup.repository}")
            print(f"    Schedule:   {backup.schedule or '–'}")
            print(f"    Next run:   {next_run_str}")
            print()
    else:
        print("  Backups: (none)")
        print()

    if job_config.rclone:
        print("  Rclone tasks:")
        for rclone_name, rclone in job_config.rclone.items():
            if rclone.schedule:
                rclone_next_str = _next_run_display(rclone.schedule)
            else:
                rclone_next_str = "manual only"
            print(f"  {rclone_name}")
            print(f"    Source:   {rclone.source}")
            print(f"    Target:   {rclone.target}")
            print(f"    Schedule: {rclone.schedule or '–'}")
            print(f"    Next run: {rclone_next_str}")
            print()
    else:
        print("  Rclone tasks: (none)")

    return EXIT_SUCCESS


def cmd_next_runs(args: argparse.Namespace, config_path: Path) -> int:
    """Shows upcoming scheduled runs sorted by next execution time.

    Args:
        args: Parsed CLI arguments (contains optional args.job_name).
        config_path: Path to TOML config file.

    Returns:
        Exit code: 0 success, 2 config error.
    """
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        print(f"✗ Config file not found: {config_path}")
        return EXIT_CONFIG_ERROR
    except (tomllib.TOMLDecodeError, ValidationError, ValueError) as exc:
        print(f"✗ Config error: {exc}")
        return EXIT_CONFIG_ERROR

    runs: list[dict[str, Any]] = []

    job_name_filter: str | None = getattr(args, "job_name", None)
    if job_name_filter is not None:
        job = config.jobs.get(job_name_filter)
        if job is None:
            print(f"✗ Job '{job_name_filter}' not found in config")
            return EXIT_CONFIG_ERROR
        jobs_to_show = {job_name_filter: job}
    else:
        jobs_to_show = config.jobs

    for job_name, job_config in jobs_to_show.items():
        for backup_name, backup in job_config.backup.items():
            if not backup.schedule:
                continue
            target = f"{job_name}.backup.{backup_name}"
            next_run = _next_run_or_error(backup.schedule, target)
            if next_run is None:
                return EXIT_CONFIG_ERROR
            runs.append(
                {
                    "next_run": next_run,
                    "job": job_name,
                    "name": f"backup.{backup_name}",
                    "type": "backup",
                }
            )
        for wf_name, wf in job_config.workflows.items():
            if not wf.schedule:
                continue
            target = f"{job_name}.workflow.{wf_name}"
            next_run = _next_run_or_error(wf.schedule, target)
            if next_run is None:
                return EXIT_CONFIG_ERROR
            runs.append(
                {
                    "next_run": next_run,
                    "job": job_name,
                    "name": f"workflow.{wf_name}",
                    "type": "workflow",
                }
            )
        for rclone_name, task in job_config.rclone.items():
            if not task.schedule:
                continue
            target = f"{job_name}.rclone.{rclone_name}"
            next_run_rclone = _next_run_or_error(task.schedule, target)
            if next_run_rclone is None:
                return EXIT_CONFIG_ERROR
            runs.append(
                {
                    "next_run": next_run_rclone,
                    "job": job_name,
                    "name": f"rclone.{rclone_name}",
                    "type": "rclone",
                }
            )

    runs.sort(key=lambda r: r["next_run"])

    if not runs:
        print("No scheduled runs configured.")
        return EXIT_SUCCESS

    for run in runs:
        print(
            f"{run['next_run'].strftime('%Y-%m-%d %H:%M')}  "
            f"{run['job']}.{run['name']}  "
            f"({run['type']})"
        )

    return EXIT_SUCCESS


def cmd_show_logs(args: argparse.Namespace) -> int:
    """Zeigt Log-Dateien eines Jobs an.

    Args:
        args: Geparste CLI-Argumente (enthält args.job_name, args.days, args.tail).

    Returns:
        Exit-Code: 0 Erfolg, 1 Fehler.
    """
    job_name: str = args.job_name
    days: int = args.days
    tail_n: int | None = args.tail

    log_dir = resolve_job_log_dir(job_name, _effective_log_base_dir())
    if log_dir is None:
        print(f"✗ Invalid job name '{job_name}'")
        return EXIT_ERROR
    if not log_dir.exists():
        print(f"No logs found for job '{job_name}' (directory does not exist: {log_dir})")
        return EXIT_ERROR

    today = datetime.now().date()
    dates = [today - timedelta(days=i) for i in range(days)]

    if tail_n is not None:
        tail_lines: deque[str] = deque(maxlen=max(tail_n, 0))
        found_any = False
        for date in reversed(dates):
            log_file = log_dir / f"{date.strftime('%Y-%m-%d')}.log"
            if log_file.exists():
                found_any = True
                try:
                    lines, _truncated = tail_file_lines(log_file, tail_n)
                except OSError as exc:
                    print(f"✗ Could not read {log_file}: {exc}")
                    return EXIT_ERROR
                tail_lines.extend(lines)
        if not found_any:
            print(f"No log files found for job '{job_name}' in the last {days} day(s)")
            return EXIT_SUCCESS
        for line in tail_lines:
            print(line.rstrip("\n"))
        return EXIT_SUCCESS

    found_any = False
    for date in dates:
        log_file = log_dir / f"{date.strftime('%Y-%m-%d')}.log"
        if not log_file.exists():
            continue
        found_any = True
        date_str = date.strftime("%Y-%m-%d")
        print(f"=== Logs for {job_name} ({date_str}) ===\n")
        try:
            content = log_file.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"✗ Could not read {log_file}: {exc}")
            return EXIT_ERROR
        print(content if content.endswith("\n") else content + "\n")

    if not found_any:
        print(f"No log files found for job '{job_name}' in the last {days} day(s)")

    return EXIT_SUCCESS


def cmd_tail_logs(args: argparse.Namespace) -> int:
    """Folgt den Logs eines Jobs in Echtzeit (wie tail -f).

    Args:
        args: Geparste CLI-Argumente (enthält args.job_name).

    Returns:
        Exit-Code: 0 Erfolg, 1 Fehler.
    """
    job_name: str = args.job_name

    log_dir = resolve_job_log_dir(job_name, _effective_log_base_dir())
    if log_dir is None:
        print(f"✗ Invalid job name '{job_name}'")
        return EXIT_ERROR
    if not log_dir.exists():
        print(f"No logs found for job '{job_name}' (directory does not exist: {log_dir})")
        return EXIT_ERROR

    today = datetime.now().date()
    log_file = log_dir / f"{today.strftime('%Y-%m-%d')}.log"

    if not log_file.exists():
        existing = sorted(log_dir.glob("*.log"))
        if not existing:
            print(f"No log files found for job '{job_name}'")
            return EXIT_ERROR
        log_file = existing[-1]

    print(f"Following {log_file} (Ctrl+C to stop)\n", flush=True)

    try:
        while True:
            with open(log_file, encoding="utf-8") as f:
                for line in f:
                    sys.stdout.write(line)
                sys.stdout.flush()
                while True:
                    line = f.readline()
                    if line:
                        sys.stdout.write(line)
                        sys.stdout.flush()
                    else:
                        time.sleep(0.1)
                        today_now = datetime.now().date()
                        if today_now != today:
                            new_log = log_dir / f"{today_now.strftime('%Y-%m-%d')}.log"
                            if new_log.exists():
                                today = today_now
                                log_file = new_log
                                print(f"\n=== Rotated to {log_file} ===\n", flush=True)
                                break
    except KeyboardInterrupt:
        pass

    return EXIT_SUCCESS


_SHELL_WIDTH = 70


def _shell_password_type(backup: ResolvedBackupConfig) -> str:
    if backup.credentials.password_env is not None:
        return f"password_env: {backup.credentials.password_env}"
    if backup.credentials.password_file is not None:
        return "password_file"
    if backup.credentials.password is not None:
        return "password"
    return "no password configured"


def _print_shell_banner(title: str, lines: list[str]) -> None:
    """Prints a framed banner to stderr before spawning the shell."""
    top_fill = "─" * max(0, _SHELL_WIDTH - 2 - len(title))
    bot_label = " Type 'exit' to leave "
    bot_fill = "─" * max(0, _SHELL_WIDTH - 2 - len(bot_label))
    print(f"┌─{title}{top_fill}", file=sys.stderr)
    for line in lines:
        print(f"│  {line}", file=sys.stderr)
    print(f"└─{bot_label}{bot_fill}", file=sys.stderr)


def _spawn_shell(env: dict[str, str]) -> int:
    """Spawns an interactive shell with the given environment."""
    shell = os.environ.get("SHELL", "/bin/bash")
    result = subprocess.run([shell], env=env)
    return result.returncode


def _shell_overview_entries(
    job_name: str,
    job_config: ResolvedJobConfig,
) -> list[tuple[str, str, str]]:
    entries: list[tuple[str, str, str]] = []

    if job_config.backup:
        for backup_name, backup in job_config.backup.items():
            cmd = f"dk shell {job_name}.backup.{backup_name}"
            pw_type = f"({_shell_password_type(backup)})"
            entries.append((cmd, backup.repository, pw_type))

    for rclone_name, task in job_config.rclone.items():
        cmd = f"dk shell {job_name}.rclone.{rclone_name}"
        endpoint = f"{task.source} → {task.target}"
        entries.append((cmd, endpoint, ""))

    return entries


def _print_shell_overview(job_name: str, entries: list[tuple[str, str, str]]) -> None:
    """Print shell target rows for one job."""
    cmd_width = max(len(e[0]) for e in entries)
    repo_width = max(len(e[1]) for e in entries)

    print(f"\nAvailable tasks for '{job_name}':\n")
    for cmd, repo, pw_type in entries:
        line = f"  {cmd:<{cmd_width}}   {repo:<{repo_width}}"
        if pw_type:
            line += f"   {pw_type}"
        print(line)
    print()


def _cmd_shell_overview(job_name: str, config: ResolvedAppConfig) -> int:
    """Lists all available shell targets for a job."""
    job_config = config.jobs.get(job_name)
    if job_config is None:
        print(f"✗ Job '{job_name}' not found in config")
        return EXIT_CONFIG_ERROR

    entries = _shell_overview_entries(job_name, job_config)
    if not entries:
        print(f"No backups or rclone tasks configured for job '{job_name}'.")
        return EXIT_SUCCESS

    _print_shell_overview(job_name, entries)

    return EXIT_SUCCESS


def _cmd_shell_all_overview(config: ResolvedAppConfig) -> int:
    """Lists all available shell targets across all jobs."""
    if not config.jobs:
        print("No jobs configured.")
        return EXIT_SUCCESS

    printed_any = False
    for job_name in sorted(config.jobs):
        entries = _shell_overview_entries(job_name, config.jobs[job_name])
        if not entries:
            continue
        _print_shell_overview(job_name, entries)
        printed_any = True

    if not printed_any:
        print("No backups or rclone tasks configured.")

    return EXIT_SUCCESS


def _cmd_shell_backup(job_name: str, backup_name: str, backup: ResolvedBackupConfig) -> int:
    """Spawns a shell with RESTIC_REPOSITORY and password env vars set."""
    env = resolve_env(
        password=backup.credentials.password,
        password_file=backup.credentials.password_file,
    )
    env["RESTIC_REPOSITORY"] = backup.repository

    lines: list[str] = [f"RESTIC_REPOSITORY    = {backup.repository}"]
    if backup.credentials.password_file is not None:
        lines.append(f"RESTIC_PASSWORD_FILE = {backup.credentials.password_file}")
    elif backup.credentials.password is not None:
        lines.append("RESTIC_PASSWORD      = ********")
    elif backup.credentials.password_env is not None:
        lines.append(
            f"WARNING: password_env '{backup.credentials.password_env}' is configured "
            "but was not set (or empty) when the config was loaded — no restic "
            "password is available in this shell."
        )
    lines.append("")
    lines.append("Example commands:")
    lines.append("  restic snapshots")
    lines.append("  restic ls latest")
    if backup.input.sources:
        lines.append(f"  restic backup {' '.join(backup.input.sources)}")
    lines.append("  restic check")
    lines.append("  restic stats")

    _print_shell_banner(f" {job_name} / {backup_name} ", lines)
    return _spawn_shell(env)


def _cmd_shell_rclone(
    job_name: str, rclone_name: str, rclone_task: ResolvedRcloneSyncTaskConfig
) -> int:
    """Spawns a shell with rclone context info printed."""
    lines: list[str] = [
        f"Source:  {rclone_task.source}",
        f"Target:  {rclone_task.target}",
        "",
        "Example commands:",
        f"  rclone ls {rclone_task.target}",
        f"  rclone sync {rclone_task.source} {rclone_task.target}",
        f"  rclone check {rclone_task.source} {rclone_task.target}",
        f"  rclone lsd {rclone_task.target}",
    ]

    _print_shell_banner(f" {job_name} / rclone.{rclone_name} ", lines)
    return _spawn_shell(dict(os.environ))


def cmd_shell(args: argparse.Namespace, config_path: Path) -> int:
    """Öffnet eine pre-konfigurierte Shell für ein Job-Backup oder rclone-Task.

    Args:
        args: Geparste CLI-Argumente (enthält args.task_selector).
        config_path: Pfad zur TOML-Konfigurationsdatei.

    Returns:
        Exit-Code: 0 Erfolg, 2 Config-Fehler, oder Shell-Exit-Code.
    """
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        print(f"✗ Config file not found: {config_path}")
        return EXIT_CONFIG_ERROR
    except (tomllib.TOMLDecodeError, ValidationError, ValueError) as exc:
        print(f"✗ Config error: {exc}")
        return EXIT_CONFIG_ERROR

    task_selector: str | None = getattr(args, "task_selector", None)
    if task_selector is None:
        return _cmd_shell_all_overview(config)

    if "." not in task_selector:
        return _cmd_shell_overview(task_selector, config)

    try:
        parsed = parse_task_selector(task_selector)
    except ValueError as exc:
        print(f"✗ {exc}")
        return EXIT_CONFIG_ERROR

    job_name = parsed.job
    job_config = config.jobs.get(job_name)
    if job_config is None:
        print(f"✗ Job '{job_name}' not found in config")
        return EXIT_CONFIG_ERROR

    if parsed.kind == "rclone":
        if parsed.name not in job_config.rclone:
            print(f"✗ Rclone task '{task_selector}' not found in job '{job_name}'")
            return EXIT_CONFIG_ERROR
        return _cmd_shell_rclone(job_name, parsed.name, job_config.rclone[parsed.name])

    if parsed.kind == "backup":
        if parsed.name not in job_config.backup:
            print(f"✗ Backup '{task_selector}' not found in job '{job_name}'")
            return EXIT_CONFIG_ERROR
        return _cmd_shell_backup(job_name, parsed.name, job_config.backup[parsed.name])

    print(
        f"✗ Task '{task_selector}' not found in job '{job_name}'. "
        "Use 'backup.<name>' or 'rclone.<name>'"
    )
    return EXIT_CONFIG_ERROR


def cmd_gui(args: argparse.Namespace, config_path: Path) -> int:
    """Startet den GUI-Runtime-Server.

    Args:
        args: Geparste CLI-Argumente (host, port).
        config_path: Pfad zur TOML-Konfigurationsdatei.

    Returns:
        Exit-Code: 0 nach Beenden, 1 bei Fehler.
    """
    setup_system_logger()
    mode, invalid_raw = _resolve_dk_mode()
    if invalid_raw is not None:
        _log_invalid_dk_mode(invalid_raw)
    if mode == "cli":
        print("✗ dk-runtime gui is not allowed when DK_MODE=cli. Use DK_MODE=gui or unset it.")
        return EXIT_ERROR

    try:
        import uvicorn

        from .gui.app import create_app
    except ImportError:
        print("✗ GUI dependencies are missing. Please install:")
        print("  pip install fastapi uvicorn jinja2 python-multipart sse-starlette")
        return EXIT_ERROR

    app = create_app(config_path)
    print(f"✓ GUI started: http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)
    return EXIT_SUCCESS


def cmd_scheduler(config_path: Path) -> int:
    """Startet den Scheduler-Daemon.

    Lädt die Config, registriert Signal-Handler für SIGTERM/SIGINT und
    blockiert bis ein Stop-Signal eintrifft.

    Args:
        config_path: Pfad zur TOML-Konfigurationsdatei.

    Returns:
        Exit-Code: 0 Erfolg, 1 sonstiger Startfehler (z. B. Run-Control-Socket
        konnte nicht gestartet werden), 2 Config-Fehler, 3 Scheduler-Lock belegt.
    """
    logger = setup_system_logger()
    mode, invalid_raw = _resolve_dk_mode()
    if invalid_raw is not None:
        _log_invalid_dk_mode(invalid_raw)
    if mode == "gui":
        logger.error(
            "dk-runtime scheduler requires DK_MODE=cli. "
            "Use dk-runtime gui for the default runtime."
        )
        return EXIT_ERROR

    try:
        config = load_config(config_path)
    except FileNotFoundError:
        logger.error("Config file not found: %s", config_path)
        return EXIT_CONFIG_ERROR
    except tomllib.TOMLDecodeError as exc:
        logger.error("Invalid TOML in config file: %s", exc)
        return EXIT_CONFIG_ERROR
    except ValidationError as exc:
        logger.error("Config validation failed:\n%s", exc)
        return EXIT_CONFIG_ERROR
    except ValueError as exc:
        logger.error("Config error: %s", exc)
        return EXIT_CONFIG_ERROR
    logger = setup_system_logger(_config_log_level(config))

    scheduler = CronScheduler(
        config,
        config_path=config_path,
        owner="scheduler-cli",
        status_writer=SchedulerStatusWriter(),
    )

    def _handle_signal(signum: int, _frame: object) -> None:
        sig_name = signal.Signals(signum).name
        logger.info("Received %s, stopping scheduler...", sig_name)
        scheduler.stop()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info("Scheduler daemon starting (config: %s)", config_path)
    try:
        result = scheduler.start()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Scheduler failed unexpectedly")
        logger.error("Scheduler failed to start: %s", exc)
        return EXIT_ERROR
    if result.state == SchedulerStartState.RUNNING:
        return EXIT_SUCCESS
    if result.state == SchedulerStartState.SCHEDULER_LOCK_ERROR:
        logger.error(
            "Scheduler lock is already held by another process: %s",
            result.message or "scheduler lock conflict",
        )
        return EXIT_LOCK_ERROR
    logger.error(
        "Scheduler failed to start: %s",
        result.message or "unexpected startup failure",
    )
    return EXIT_ERROR


def cmd_scheduler_status() -> int:
    """Print scheduler status and return the status-specific exit code."""
    reader = SchedulerStatusReader()
    try:
        status = reader.read(strict=True)
    except Exception as exc:  # noqa: BLE001
        print(f"Could not read scheduler status: {exc}", file=sys.stderr)
        return EXIT_ERROR

    print(json.dumps(status.to_dict(), indent=2, sort_keys=True))
    return EXIT_SUCCESS if status.state == "running" else EXIT_ERROR


def _format_run_timestamp(value: object) -> str:
    """Format a serialized run timestamp for tabular display.

    Args:
        value: The serialized timestamp value, typically an ISO 8601 string or
            ``None``.

    Returns:
        The string value, or ``"-"`` when no timestamp is present.
    """
    if value is None:
        return "-"
    return str(value)


def _format_run_target_from_payload(run: dict[str, object]) -> str:
    run_kind = run.get("run_kind")
    job = run.get("job")
    task_type = run.get("task_type")
    task_name = run.get("task_name")
    if run_kind is not None and job is not None and task_type is not None and task_name is not None:
        try:
            return format_run_target(
                RunKind(str(run_kind)),
                str(job),
                str(task_type),
                str(task_name),
            )
        except ValueError:
            pass
    return "-"


def cmd_list_runs(args: argparse.Namespace) -> int:
    """List operational runs of the active scheduler runtime.

    Queries the local scheduler control plane over its Unix-domain socket and
    prints one line per run with its identifier, origin, task, status and
    start/finish timestamps.

    Args:
        args: Parsed CLI arguments (unused; present for dispatch symmetry).

    Returns:
        Exit code: 0 success, 1 when no scheduler runtime is reachable.
    """
    del args
    client = RunControlClient(default_socket_path())
    try:
        runs = asyncio.run(client.list_runs())
    except ServiceError as exc:
        print(
            f"No scheduler runtime reachable (is the scheduler running?): {exc.message}",
            file=sys.stderr,
        )
        return EXIT_ERROR

    if not runs:
        print("No scheduler runs.")
        return EXIT_SUCCESS

    columns = ("RUN ID", "ORIGIN", "TASK", "STATUS", "STARTED / FINISHED")
    print("  ".join(columns))
    for run in runs:
        run_id = str(run.get("run_id", "-"))
        origin = str(run.get("origin", "-"))
        target = _format_run_target_from_payload(run)
        status = str(run.get("status", "-"))
        started = _format_run_timestamp(run.get("started_at"))
        finished = _format_run_timestamp(run.get("finished_at"))
        cells = (run_id, origin, target, status, f"{started} / {finished}")
        print("  ".join(cells))

    return EXIT_SUCCESS


def cmd_cancel_run(args: argparse.Namespace) -> int:
    """Cancel a scheduler run by its run id via the control plane.

    Sends a cancel request to the local scheduler control-plane socket and
    prints an honest message reflecting what actually happened. ``RunManager
    .cancel()`` is idempotent and may return the run record unchanged when the
    run is already terminal or no longer cancellable, so the printed message
    differentiates between these cases instead of always claiming "cancelled".

    Args:
        args: Parsed CLI arguments (contains args.run_id).

    Returns:
        Exit code: 0 success (scheduler found the run, regardless of whether
        cancellation was actually triggered), 1 on unknown run id or
        unreachable scheduler.
    """
    run_id: str = args.run_id
    client = RunControlClient(default_socket_path())
    try:
        result = asyncio.run(client.cancel_run(run_id))
    except NotFoundServiceError:
        print(f"Run not found: {run_id}", file=sys.stderr)
        return EXIT_ERROR
    except ServiceError as exc:
        print(
            f"No scheduler runtime reachable (is the scheduler running?): {exc.message}",
            file=sys.stderr,
        )
        return EXIT_ERROR

    status = str(result.get("status", "unknown"))
    cancellable = bool(result.get("cancellable", False))
    if status not in ("queued", "running"):
        print(f"Run {run_id} already finished (status: {status}); nothing to cancel")
    elif cancellable:
        print(f"Cancellation requested for run {run_id} (status: {status})")
    else:
        print(
            f"Run {run_id} is no longer cancellable (status: {status}); "
            "it is finishing post-processing"
        )
    return EXIT_SUCCESS


def main() -> None:
    """Haupteinstiegspunkt des CLI.

    Parst Argumente, wendet den Modus-Guard an (im GUI-Modus sind nur
    ``shell`` und ``config validate`` erlaubt) und delegiert an den passenden
    Command-Handler. Beendet den Prozess mit dem entsprechenden Exit-Code.
    """
    mode, invalid_raw = _resolve_dk_mode()
    parser = _build_parser(mode)
    if invalid_raw is not None:
        _log_invalid_dk_mode(invalid_raw)
    first_command = _first_command_token(sys.argv[1:])
    if mode == "gui" and first_command in _GUI_MODE_BLOCKED_GROUPS:
        print(_GUI_MODE_BLOCKED_MESSAGE)
        sys.exit(EXIT_ERROR)

    args = parser.parse_args()
    if not hasattr(args, "command"):
        parser.print_help()
        sys.exit(EXIT_SUCCESS)
    if mode == "gui" and args.command not in _GUI_MODE_ALLOWED_COMMANDS:
        print(_GUI_MODE_BLOCKED_MESSAGE)
        sys.exit(EXIT_ERROR)

    config_path: Path = args.config

    if args.command == "run":
        sys.exit(cmd_run_job(args, config_path))
    elif args.command == "config-validate":
        sys.exit(cmd_validate_config(config_path))
    elif args.command == "jobs-list":
        sys.exit(cmd_list_jobs(args, config_path))
    elif args.command == "jobs-workflows":
        sys.exit(cmd_list_workflows(args, config_path))
    elif args.command == "jobs-tasks":
        sys.exit(cmd_list_tasks(args, config_path))
    elif args.command == "schedule-next":
        sys.exit(cmd_next_runs(args, config_path))
    elif args.command == "logs-show":
        sys.exit(cmd_show_logs(args))
    elif args.command == "logs-tail":
        sys.exit(cmd_tail_logs(args))
    elif args.command == "scheduler_status":
        sys.exit(cmd_scheduler_status())
    elif args.command == "runs-list":
        sys.exit(cmd_list_runs(args))
    elif args.command == "runs-cancel":
        sys.exit(cmd_cancel_run(args))
    elif args.command == "shell":
        sys.exit(cmd_shell(args, config_path))
    else:
        parser.print_help()
        sys.exit(EXIT_ERROR)


if __name__ == "__main__":
    main()
