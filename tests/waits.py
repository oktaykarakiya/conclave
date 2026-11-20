"""How every test waits for what happens in the background: for the condition itself.

A background task's database writes, a subprocess's file, a subscriber, an event: how long they
take depends on how busy the host is (another project compiling makes a millisecond step take
seconds), so a test never sleeps a fixed time and then asserts that they happened. It waits for
them with :func:`wait_until` or :func:`within`, whose deadline only bounds a failure and is
therefore generous: a passing test waits only as long as the condition takes (and a poll more).

(``tests/`` is on ``sys.path`` through its ``conftest.py``, so unit and integration tests both
import this module as ``waits``.)
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable

DEADLINE_S = 60.0
"""How long a test waits for what it expects before it fails."""

_FIRST_PAUSE_S = 0.001
_LONGEST_PAUSE_S = 0.05


async def wait_until[T](
    check: Callable[[], T | Awaitable[T]], failure: str, *, seconds: float = DEADLINE_S
) -> T:
    """Call *check* (a function or a coroutine function) until it returns a truthy value, and
    return that value. After *seconds*, fail with *failure*, which says what never happened.

    It checks at once, then after pauses growing from a millisecond to 50 ms, so a quick
    condition is seen quickly and a slow one is not polled so often that the polling slows the
    work it waits for.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    pause = _FIRST_PAUSE_S
    while True:
        result = check()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return result
        if loop.time() >= deadline:
            raise AssertionError(f"{failure} (waited {seconds:g} s)")
        await asyncio.sleep(pause)
        pause = min(2 * pause, _LONGEST_PAUSE_S)


async def within[T](
    awaitable: Awaitable[T], failure: str, *, seconds: float = DEADLINE_S
) -> T:
    """Await *awaitable* (an event's ``wait()``, a task, a subscription's next event) and return
    its result. After *seconds*, cancel it and fail with *failure*, which says what never
    happened."""
    deadline = asyncio.timeout(seconds)
    try:
        async with deadline:
            return await awaitable
    except TimeoutError:
        if not deadline.expired():  # raised by the awaitable itself, not the deadline
            raise
        raise AssertionError(f"{failure} (waited {seconds:g} s)") from None
