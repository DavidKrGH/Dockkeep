"""Rclone utility helpers.

Provides standalone functions for common rclone operations:
remote name parsing, listing configured remotes, and checking whether a
remote is reachable. These helpers are used by the rclone executor but are
kept separate so they can also be called from CLI commands and integration
tests without instantiating a full executor.
"""

import logging

from ..core.subprocesses import run_command

logger = logging.getLogger(__name__)


def parse_remote_name(remote: str) -> str:
    """Extracts the rclone remote name from a remote path.

    Rclone remote paths have the form ``<name>:<path>`` (e.g.
    ``gdrive:backups/home`` or ``s3:mybucket``). This function returns the
    part before the first colon.

    Args:
        remote: Full rclone remote path (e.g. ``gdrive:backups/home``).

    Returns:
        The remote name without the colon or path suffix.

    Raises:
        ValueError: If *remote* does not contain a colon.

    Example:
        >>> parse_remote_name("gdrive:backups/home")
        'gdrive'
        >>> parse_remote_name("s3:mybucket")
        's3'
    """
    if ":" not in remote:
        raise ValueError(f"Invalid rclone remote path '{remote}': expected '<name>:<path>' format")
    return remote.split(":", 1)[0]


async def async_list_remotes(timeout: int | None = None) -> list[str]:
    """Returns configured rclone remote names without blocking the event loop.

    Args:
        timeout: Optional timeout in seconds for ``rclone listremotes``.

    Returns:
        List of configured remote names without trailing colons. Returns an
        empty list when rclone cannot be started, times out, or exits nonzero.
    """
    cmd = ["rclone", "listremotes"]
    logger.debug("Listing configured rclone remotes")

    try:
        result = await run_command(cmd, timeout=timeout)
    except TimeoutError:
        logger.error("rclone listremotes timed out after %ss", timeout)
        return []
    except FileNotFoundError:
        logger.error("rclone executable not found")
        return []
    except OSError as exc:
        logger.error("Failed to start rclone: %s", exc)
        return []

    if result.returncode != 0:
        if result.stderr:
            for line in result.stderr.splitlines():
                logger.error("[stderr] %s", line)
        logger.error("rclone listremotes failed (exit %d)", result.returncode)
        return []

    remotes = _parse_list_remotes_output(result.stdout)
    logger.debug("Found %d configured remote(s): %s", len(remotes), remotes)
    return remotes


def _parse_list_remotes_output(stdout: str) -> list[str]:
    """Parses ``rclone listremotes`` output into remote names."""
    remotes: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.endswith(":"):
            remotes.append(line[:-1])
        elif line:
            remotes.append(line)
    return remotes


async def async_check_remote(remote: str, timeout: int | None = None) -> bool:
    """Checks asynchronously whether an rclone remote is configured.

    Args:
        remote: Full rclone remote path (e.g. ``gdrive:backups/home``).
        timeout: Optional timeout in seconds for ``rclone listremotes``.

    Returns:
        True if the remote name is present in the rclone config, False otherwise.
    """
    try:
        remote_name = parse_remote_name(remote)
    except ValueError as exc:
        logger.error("Invalid remote path: %s", exc)
        return False

    configured = await async_list_remotes(timeout=timeout)
    if remote_name in configured:
        logger.debug("Remote '%s' is configured", remote_name)
        return True

    logger.warning(
        "Remote '%s' is not configured (configured remotes: %s)",
        remote_name,
        configured,
    )
    return False
