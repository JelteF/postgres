# Copyright (c) 2025, PostgreSQL Global Development Group

"""
Tests for the GoAway protocol message during smart shutdown.

The GoAway message is sent by the server during smart shutdown to politely
request that clients disconnect when convenient. The connection remains
functional after receiving the message.
"""


def test_goaway_smart_shutdown(pg, wait_until):
    """
    Test that GoAway message is sent during smart shutdown.

    This test:
    1. Connects to a running PostgreSQL server via Unix socket
    2. Verifies GoAway is not received initially
    3. Initiates a smart shutdown
    4. Verifies that GoAway is received
    5. Verifies that queries still work after GoAway
    """

    # Connect to the server via Unix socket, libpq will request the
    # _pq_.goaway protocol extension
    conn = pg.connect(max_protocol_version="latest")

    # Initially, GoAway should not be received
    assert not conn.goaway_received(), "GoAway should not be received initially"

    # Execute a simple query to ensure connection is working
    conn.sql("SELECT 1")

    pg.pg_ctl("stop", "--mode", "smart", "--no-wait")

    for _ in wait_until("Did not receive GoAway after smart shutdown"):
        # Consume any data the backend may have sent (like GoAway)
        assert conn.consume_input()
        if conn.goaway_received():
            break

    # Connection should still be functional after receiving GoAway
    conn.sql("SELECT 2")
    conn.sql("SELECT 3")

    # Verify GoAway is still flagged
    assert conn.goaway_received(), "GoAway flag should remain set"
