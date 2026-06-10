# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/authentication/t/007_pre_auth.pl.

Tests connection behavior prior to authentication: a backend parked at the
``init-pre-auth`` injection point (which fires during startup, just before
authentication) must already be visible in pg_stat_activity with
``state = 'starting'`` and ``wait_event = 'init-pre-auth'``, and once woken it
must reach ``idle``.

GOTCHA: while ``init-pre-auth`` is attached in ``wait`` mode, *every* new
connection hangs in startup. All observation therefore goes through a single
control connection opened *before* the point is attached; we must never use
``node.sql()`` / ``poll_query_until()`` here, since those open fresh
connections that would themselves hang.
"""

import time
from concurrent.futures import ThreadPoolExecutor

from pypg import skip_unless_injection_points
from pypg._env import test_timeout_default as timeout_default


def _poll(ctl, query, timeout):
    """Poll ``query`` over the pre-existing control connection until it returns
    a truthy (non-empty) result, then return it."""
    deadline = time.monotonic() + timeout
    while True:
        result = ctl.sql(query)
        if result != []:
            return result
        assert time.monotonic() < deadline, f"timed out waiting for: {query}"
        time.sleep(0.1)


def test_pre_auth(create_pg):
    node = create_pg("primary", conf=["log_connections = 'receipt,authentication'"])
    skip_unless_injection_points(node)
    node.sql("CREATE EXTENSION injection_points")

    # Control connection, established before any waitpoint exists so it does not
    # hang. All server interaction below goes through it.
    ctl = node.background()
    ctl.sql("SELECT injection_points_attach('init-pre-auth', 'wait')")

    # From this point on, all new connections hang during startup, just before
    # authentication. connect() is synchronous, so drive it from a worker
    # thread and collect the connection once it is woken.
    with ThreadPoolExecutor(max_workers=1) as executor:
        conn_future = executor.submit(node.connect)

        # Wait for the connection to show up in pg_stat_activity parked at the
        # injection point.
        pid = _poll(
            ctl,
            "SELECT pid FROM pg_stat_activity "
            "WHERE backend_type = 'client backend' "
            "AND state = 'starting' "
            "AND wait_event = 'init-pre-auth'",
            timeout_default(),
        )

        # Detach the waitpoint and wait for the connection to complete.
        ctl.sql("SELECT injection_points_wakeup('init-pre-auth')")
        conn = conn_future.result(timeout=timeout_default())

    # Make sure the pgstat entry is updated eventually.
    _poll(
        ctl,
        f"SELECT 1 FROM pg_stat_activity WHERE pid = {pid} AND state = 'idle'",
        timeout_default(),
    )

    ctl.sql("SELECT injection_points_detach('init-pre-auth')")
    conn.close()
