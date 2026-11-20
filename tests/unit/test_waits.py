"""The waits every test uses for background work (``tests/waits.py``): what they return, and how
they fail when what a test waits for never happens."""

from __future__ import annotations

import asyncio

import pytest

from waits import DEADLINE_S, wait_until, within


async def test_wait_until_returns_the_first_truthy_result_of_a_sync_or_async_check() -> None:
    answers = iter([None, "", "ready", "later"])

    assert await wait_until(lambda: next(answers), "never ready") == "ready"

    calls = 0

    async def third_call() -> int:
        nonlocal calls
        calls += 1
        return calls if calls == 3 else 0

    assert await wait_until(third_call, "never called three times") == 3


async def test_wait_until_names_what_never_happened() -> None:
    with pytest.raises(AssertionError, match=r"^the file never appeared \(waited 0.05 s\)$"):
        await wait_until(lambda: False, "the file never appeared", seconds=0.05)


async def test_within_returns_the_result_or_cancels_and_names_what_never_happened() -> None:
    assert await within(asyncio.sleep(0, "done"), "it never finished") == "done"

    never = asyncio.create_task(asyncio.Event().wait())
    with pytest.raises(AssertionError, match=r"^the event never came \(waited 0.05 s\)$"):
        await within(never, "the event never came", seconds=0.05)
    assert never.cancelled()


async def test_within_leaves_a_timeout_the_awaitable_raised_itself_alone() -> None:
    async def times_out() -> None:
        raise TimeoutError("its own")

    with pytest.raises(TimeoutError, match="its own"):
        await within(times_out(), "never reported as a missed deadline")


def test_the_deadline_is_generous() -> None:
    assert DEADLINE_S >= 10
