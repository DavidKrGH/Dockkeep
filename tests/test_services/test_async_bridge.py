from __future__ import annotations

import asyncio

import pytest

from src.services.async_bridge import run_sync


async def _value(value: str) -> str:
    await asyncio.sleep(0)
    return value


def test_run_sync_runs_coroutine_without_running_loop() -> None:
    assert run_sync(_value("done")) == "done"


def test_run_sync_runs_coroutine_from_running_loop() -> None:
    async def caller() -> str:
        return run_sync(_value("threaded"))

    assert asyncio.run(caller()) == "threaded"


async def _none() -> None:
    await asyncio.sleep(0)
    return None


def test_run_sync_passes_through_none_without_running_loop() -> None:
    assert run_sync(_none()) is None


def test_run_sync_passes_through_none_from_running_loop() -> None:
    async def caller() -> None:
        return run_sync(_none())

    assert asyncio.run(caller()) is None


def test_run_sync_propagates_coroutine_errors_from_running_loop() -> None:
    async def fail() -> None:
        await asyncio.sleep(0)
        raise ValueError("boom")

    async def caller() -> None:
        run_sync(fail())

    with pytest.raises(ValueError, match="boom"):
        asyncio.run(caller())
