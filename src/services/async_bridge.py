"""Small sync-to-async bridge for service methods."""

import asyncio
import threading
from collections.abc import Coroutine
from typing import TypeVar, cast

T = TypeVar("T")

_UNSET: object = object()


def run_sync(coro: Coroutine[object, object, T]) -> T:
    """Run a coroutine from synchronous service APIs.

    If the caller already owns an event loop, execute the coroutine in a short
    helper thread with its own loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: object = _UNSET
    error: BaseException | None = None

    def runner() -> None:
        nonlocal result, error
        try:
            result = asyncio.run(coro)
        except BaseException as exc:  # pragma: no cover - defensive compatibility path
            error = exc

    thread = threading.Thread(target=runner)
    thread.start()
    thread.join()
    if error is not None:
        raise error
    if result is _UNSET:  # pragma: no cover - defensive: thread always sets one branch
        raise RuntimeError("run_sync helper thread did not produce a result")
    return cast(T, result)
