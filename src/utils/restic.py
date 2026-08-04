"""Restic utility helpers.

Provides standalone functions for common restic operations:
password resolution, repository existence checks, and repository
initialisation. These helpers are used by the restic executors but
are kept separate so they can also be called from CLI commands and
integration tests without instantiating a full executor.
"""

import logging
import os

from ..core.subprocesses import run_command

logger = logging.getLogger(__name__)


def resolve_env(
    password: str | None = None,
    password_file: str | None = None,
) -> dict[str, str]:
    """Returns a copy of the current environment, optionally with restic password vars set.

    This helper receives the already effective credential values from runtime
    config and only translates them to restic's environment variables.

    ``password`` and ``password_file`` are mutually exclusive (enforced by the
    model validator); at most one of them will be set.

    Args:
        password: Direct password value. Sets ``RESTIC_PASSWORD`` in the
            returned environment.
        password_file: Absolute path to a file containing the restic password.
            Sets ``RESTIC_PASSWORD_FILE`` in the returned environment.

    Returns:
        Environment dict suitable for passing to child processes.

    Example:
        >>> env = resolve_env(password="secret")
        >>> env["RESTIC_PASSWORD"]
        'secret'
        >>> env = resolve_env(password_file="/run/secrets/restic-password")
        >>> env["RESTIC_PASSWORD_FILE"]
        '/run/secrets/restic-password'
    """
    env = os.environ.copy()
    env.pop("RESTIC_PASSWORD", None)
    env.pop("RESTIC_PASSWORD_FILE", None)
    env.pop("RESTIC_PASSWORD_COMMAND", None)
    if password is not None:
        env["RESTIC_PASSWORD"] = password
    if password_file is not None:
        env["RESTIC_PASSWORD_FILE"] = password_file
    return env


async def async_check_repository(
    repository: str,
    env: dict[str, str],
    timeout: int | None = None,
    log: logging.Logger | None = None,
) -> bool:
    """Checks asynchronously whether a restic repository is initialised.

    Args:
        repository: Local path or rclone remote path of the repository.
        env: Environment variables including RESTIC_PASSWORD.
        timeout: Optional subprocess timeout in seconds.
        log: Optional logger to use (e.g. a job-scoped logger so the output
            ends up in the job log). Defaults to the module logger.

    Returns:
        True if the repository is accessible, False otherwise.
    """
    log = log or logger
    cmd = ["restic", "--repo", repository, "cat", "config"]
    log.debug("Checking repository: %s", repository)

    try:
        result = await run_command(cmd, env=env, timeout=timeout)
    except TimeoutError:
        log.error("Repository check timed out after %ss: %s", timeout, repository)
        return False
    except FileNotFoundError:
        log.error("restic executable not found")
        return False
    except OSError as exc:
        log.error("Failed to start restic: %s", exc)
        return False

    if result.returncode == 0:
        log.debug("Repository is initialised: %s", repository)
        return True

    log.debug("Repository check failed (exit %d): %s", result.returncode, repository)
    return False


async def async_init_repository(
    repository: str,
    env: dict[str, str],
    timeout: int | None = None,
    log: logging.Logger | None = None,
) -> bool:
    """Initialises a new restic repository asynchronously.

    Args:
        repository: Local path or rclone remote path of the new repository.
        env: Environment variables including RESTIC_PASSWORD.
        timeout: Optional subprocess timeout in seconds.
        log: Optional logger to use (e.g. a job-scoped logger so the output
            ends up in the job log). Defaults to the module logger.

    Returns:
        True when initialisation succeeded, False otherwise.
    """
    log = log or logger
    cmd = ["restic", "--repo", repository, "init"]
    log.debug("Initialising repository: %s", repository)

    try:
        result = await run_command(cmd, env=env, timeout=timeout)
    except TimeoutError:
        log.error("Repository init timed out after %ss: %s", timeout, repository)
        return False
    except FileNotFoundError:
        log.error("restic executable not found")
        return False
    except OSError as exc:
        log.error("Failed to start restic: %s", exc)
        return False

    if result.stdout:
        for line in result.stdout.splitlines():
            log.debug("[stdout] %s", line)

    if result.returncode == 0:
        log.info("Repository initialised: %s", repository)
        return True

    if result.stderr:
        for line in result.stderr.splitlines():
            log.error("[stderr] %s", line)
    log.error(
        "Failed to initialise repository (exit %d): %s",
        result.returncode,
        repository,
    )
    return False


async def async_ensure_repository(
    repository: str,
    env: dict[str, str],
    timeout: int | None = None,
    log: logging.Logger | None = None,
) -> bool:
    """Ensures asynchronously that a repository exists.

    Args:
        repository: Local path or rclone remote path of the repository.
        env: Environment variables including RESTIC_PASSWORD.
        timeout: Optional subprocess timeout in seconds.
        log: Optional logger to use (e.g. a job-scoped logger so the output
            ends up in the job log). Defaults to the module logger.

    Returns:
        True when the repository is ready to use, False on error.
    """
    log = log or logger
    if await async_check_repository(repository, env, timeout=timeout, log=log):
        return True

    log.info("Repository not initialised, running restic init: %s", repository)
    return await async_init_repository(repository, env, timeout=timeout, log=log)
