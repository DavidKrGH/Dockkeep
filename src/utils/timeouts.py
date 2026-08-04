"""Environment-backed timeout helpers."""

import logging
import os


def env_timeout(name: str, default: int, logger: logging.Logger) -> int:
    """Return a positive timeout override from the environment.

    Args:
        name: Environment variable name.
        default: Fallback timeout in seconds.
        logger: Logger used for invalid override warnings.

    Returns:
        Positive timeout in seconds.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        timeout = int(raw)
    except ValueError:
        timeout = 0
    if timeout < 1:
        logger.warning(
            "Ignoring invalid %s=%r; expected a positive integer number of seconds. "
            "Using default %ss.",
            name,
            raw,
            default,
        )
        return default
    return timeout
