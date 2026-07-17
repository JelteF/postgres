# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/test_misc/t/005_timeouts.pl.

Tests the timeouts that cause FATAL errors (transaction, idle-in-transaction
and idle-session). It uses injection points to await the timeout deterministically
rather than relying on sleeps, which proved unstable on the buildfarm.
"""

import pypg

pytestmark = pypg.require_injection_points()


def _check_timeout(node, point, setup, log_message):
    """Arm the timeout injection point, drive a session into it from idle, then
    release it and confirm the FATAL was logged."""
    node.sql(f"SELECT injection_points_attach('{point}', 'wait')")

    # A background session sets the timeout GUC (and, where relevant, opens a
    # transaction), then goes idle. The timer armed by these short queries fires
    # while the session waits for the next command, parking the backend at the
    # injection point instead of issuing the FATAL straight away.
    session = node.connect()
    for query in setup:
        session.sql(query)

    node.wait_for_event("client backend", point)
    offset = node.current_log_position()

    node.sql(f"SELECT injection_points_wakeup('{point}')")
    node.wait_for_log(log_message, offset)
    session.close()


def test_timeouts(create_pg):
    node = create_pg("master")
    node.sql("CREATE EXTENSION injection_points")

    _check_timeout(
        node,
        "transaction-timeout",
        ["SET transaction_timeout to '10ms'", "BEGIN"],
        "terminating connection due to transaction timeout",
    )

    _check_timeout(
        node,
        "idle-in-transaction-session-timeout",
        ["SET idle_in_transaction_session_timeout to '10ms'", "BEGIN"],
        "terminating connection due to idle-in-transaction timeout",
    )

    _check_timeout(
        node,
        "idle-session-timeout",
        ["SET idle_session_timeout to '10ms'"],
        "terminating connection due to idle-session timeout",
    )
