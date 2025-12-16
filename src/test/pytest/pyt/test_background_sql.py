# Copyright (c) 2025, PostgreSQL Global Development Group

"""Tests for PGconn.background_sql(): dispatching a query that blocks while the
test carries on, plus the connection's one-query-at-a-time guarantee."""

import sys
from concurrent.futures import wait

import pytest

from libpq import LibpqError


def test_background_sql_unblocks_and_returns(pg):
    """A background query parked on a lock completes once the lock is freed,
    and its result is collected from the future."""
    holder = pg.connect()
    holder.sql("SELECT pg_advisory_lock(42)")

    waiter = pg.connect()
    fut = waiter.background_sql("SELECT pg_advisory_lock(42)")

    pg.wait_for_event("client backend", "advisory")
    holder.sql("SELECT pg_advisory_unlock(42)")

    assert fut.result() == ""  # pg_advisory_lock() returns void


def test_background_sql_keeps_session_state(pg):
    """background_sql() runs on the same connection, so a transaction it opens
    is visible to a later sql() on that connection."""
    conn = pg.connect()
    conn.background_sql("BEGIN").result()
    conn.sql("CREATE TEMP TABLE t (x int)")
    conn.sql("INSERT INTO t VALUES (1)")
    assert conn.sql("SELECT count(*) FROM t") == 1


def test_query_while_background_in_flight_raises(pg):
    """While a background query is still blocked the connection is busy, so a
    further query on it raises instead of racing the worker thread."""
    holder = pg.connect()
    holder.sql("SELECT pg_advisory_lock(43)")

    waiter = pg.connect()
    fut = waiter.background_sql("SELECT pg_advisory_lock(43)")
    pg.wait_for_event("client backend", "advisory")

    with pytest.raises(RuntimeError, match="busy with an unresolved background_sql"):
        waiter.sql("SELECT 1")

    holder.sql("SELECT pg_advisory_unlock(43)")
    fut.result()


def test_query_after_background_done_is_fine(pg):
    """Once the background query has finished the connection is free again."""
    conn = pg.connect()
    conn.background_sql("SELECT 1").result()
    assert conn.sql("SELECT 2") == 2


def test_close_with_in_flight_background_raises(pg):
    """close() guards like every other query method: closing a connection whose
    background query is still in flight raises RuntimeError — the future
    must be awaited first."""
    holder = pg.connect()
    holder.sql("SELECT pg_advisory_lock(44)")

    waiter = pg.connect()
    fut = waiter.background_sql("SELECT pg_advisory_lock(44)")
    pg.wait_for_event("client backend", "advisory")

    with pytest.raises(RuntimeError, match="busy with an unresolved background_sql"):
        waiter.close()

    # Release so the parked worker finishes; otherwise teardown's close() would
    # block joining a thread still inside libpq.
    holder.sql("SELECT pg_advisory_unlock(44)")
    fut.result()


def test_close_after_awaited_failure_is_clean(pg):
    """A background query that failed but whose result was consumed leaves the
    connection free, so close() succeeds without re-raising the error."""
    conn = pg.connect()
    fut = conn.background_sql("SELECT 1 / 0")
    with pytest.raises(LibpqError, match="division by zero"):
        fut.result()
    conn.close()


def test_close_raises_when_result_not_consumed(pg):
    """Closing with a finished but unconsumed background query raises that the
    result went unconsumed — not the query's own error, even when it failed."""
    conn = pg.connect()
    fut = conn.background_sql("SELECT 1 / 0")
    # Let it finish without consuming the result, so close() sees a finished
    # (not in-flight) future whose result was never looked at.
    wait([fut])
    with pytest.raises(RuntimeError, match="result was never consumed"):
        conn.close()
    # close() raised before finishing, so consume the future now to let
    # teardown's close() succeed instead of raising "never consumed" again.
    fut.exception()


def test_close_clean_after_consuming_success(pg):
    """Consuming a successful background query leaves close() clean."""
    conn = pg.connect()
    fut = conn.background_sql("SELECT 1")
    assert fut.result() == 1
    conn.close()


def test_exit_during_exception_does_not_raise(pg):
    """Closing while another exception is propagating must not raise the
    'result never consumed' check on top of the real failure."""
    conn = pg.connect()
    fut = conn.background_sql("SELECT 1")
    wait([fut])  # finished, but deliberately left unconsumed
    with pytest.raises(ValueError, match="boom"):
        try:
            raise ValueError("boom")
        except ValueError:
            # __exit__ sees the in-flight exception and closes without raising
            # the unconsumed-result error; the ValueError propagates unchanged.
            assert conn.__exit__(*sys.exc_info()) is None
            raise
