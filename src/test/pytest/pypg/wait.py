# Copyright (c) 2025, PostgreSQL Global Development Group

from __future__ import annotations

import time
from collections.abc import Iterator

from ._env import pg_test_timeout_default


def wait_until(
    error_message: str = "Did not complete",
    timeout: float | None = None,
    interval: float | None = None,
) -> Iterator[None]:
    """
    Loop until the timeout is reached. If the timeout is reached, raise an
    exception with the given error message.

    Use it to poll for a condition, breaking out once it holds::

        for _ in wait_until("standby did not catch up"):
            if standby.sql("SELECT ...") == expected:
                break

    The timeout defaults to PG_TEST_TIMEOUT_DEFAULT.

    By default the sleep between attempts starts at 1ms and doubles up to
    100ms. Pass ``interval`` to poll at a fixed rate instead (e.g. when each
    attempt is itself expensive).
    """
    if timeout is None:
        timeout = pg_test_timeout_default()

    start = time.time()
    end = start + timeout
    last_printed_progress = start
    sleep_for = interval if interval is not None else 0.001
    while time.time() < end:
        if timeout > 5 and time.time() - last_printed_progress > 5:
            last_printed_progress = time.time()
            print(f"{error_message} in {time.time() - start} seconds - will retry")
        yield
        time.sleep(sleep_for)
        if interval is None:
            sleep_for = min(sleep_for * 2, 0.1)

    raise TimeoutError(error_message + " in time")
