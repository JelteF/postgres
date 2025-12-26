# Copyright (c) 2025, PostgreSQL Global Development Group

"""
Tests for libpq query cancellation APIs.

These tests cover:
- PQcancel (legacy API)
- PQrequestCancel (legacy API)
- PQcancelBlocking (modern API)
- PQcancelCreate/PQcancelStart/PQcancelPoll (modern async API)
- PQcancelReset (reusing cancel connections)
"""

import time

import pytest

from libpq import ConnectionStatus
from libpq.errors import QueryCanceled


@pytest.fixture
def conn(pg):
    """Nonblocking connection for cancel tests."""
    c = pg.connect()
    c.set_nonblocking(True)
    return c


@pytest.fixture
def monitor(pg):
    """Monitor connection for observing query state."""
    return pg.connect()


def send_cancellable_query(conn, monitor, timeout=180):
    """Send a long-running query and wait for it to start executing."""
    pid = conn.backend_pid()

    # Wait for idle state first
    while (
        monitor.sql(
            f"SELECT count(*) FROM pg_stat_activity WHERE pid = {pid} AND state = 'idle'"
        )
        == 0
    ):
        time.sleep(0.01)

    # Send the query
    assert conn.send_query_params(f"SELECT pg_sleep({timeout})")

    # Wait for it to be running
    while (
        monitor.sql(
            f"SELECT count(*) FROM pg_stat_activity WHERE pid = {pid} AND wait_event = 'PgSleep'"
        )
        == 0
    ):
        time.sleep(0.01)


def consume_cancel_result(conn):
    """Consume and verify a cancelled query result."""
    with pytest.raises(QueryCanceled):
        conn.get_result().raise_error()

    while conn.is_busy():
        conn.consume_input()
    assert conn.get_result() is None


def test_pqcancel(self, conn, monitor):
    """Test the legacy PQcancel API."""
    send_cancellable_query(conn, monitor)
    conn.get_cancel().cancel()
    consume_cancel_result(conn)


def test_pqcancel_reuse(self, conn, monitor):
    """Test that PGcancel objects can be reused."""
    cancel = conn.get_cancel()
    for _ in range(2):
        send_cancellable_query(conn, monitor)
        cancel.cancel()
        consume_cancel_result(conn)


def test_pqrequestcancel(self, conn, monitor):
    """Test the legacy PQrequestCancel API."""
    send_cancellable_query(conn, monitor)
    assert conn.request_cancel()
    consume_cancel_result(conn)


def test_pqcancelblocking(self, conn, monitor):
    """Test the modern PQcancelBlocking API."""
    send_cancellable_query(conn, monitor)
    assert conn.cancel_create().blocking()
    consume_cancel_result(conn)


def test_pqcancelpoll(self, conn, monitor):
    """Test the modern async PQcancelStart/PQcancelPoll API."""
    send_cancellable_query(conn, monitor)
    cancel = conn.cancel_create()
    assert cancel.start()
    cancel.poll_until_ready()
    assert cancel.status() == ConnectionStatus.CONNECTION_OK
    consume_cancel_result(conn)


def test_pqcancelreset(self, conn, monitor):
    """Test that PGcancelConn can be reset and reused."""
    cancel = conn.cancel_create()
    for i in range(2):
        if i > 0:
            cancel.reset()
        send_cancellable_query(conn, monitor)
        assert cancel.start()
        cancel.poll_until_ready()
        assert cancel.status() == ConnectionStatus.CONNECTION_OK
        consume_cancel_result(conn)


@pytest.mark.parametrize("max_protocol_version", [None, "3.0"])
def test_pqcancel_protocol_versions(pg, max_protocol_version):
    """Test PQcancel with different protocol versions."""
    opts = (
        {"max_protocol_version": max_protocol_version} if max_protocol_version else {}
    )
    conn = pg.connect(**opts)
    conn.set_nonblocking(True)
    monitor = pg.connect()

    send_cancellable_query(conn, monitor)
    conn.get_cancel().cancel()
    consume_cancel_result(conn)
