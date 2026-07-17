# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/test_misc/t/006_signal_autovacuum.pl.

Tests signaling an autovacuum worker with pg_signal_autovacuum_worker: only
roles with privileges of pg_signal_autovacuum_worker may terminate one. An
injection point at the start of autovacuum worker startup keeps a worker parked
so it can be targeted deterministically.
"""

import pytest

import pypg
from libpq import LibpqError

POINT = "autovacuum-worker-start"

pytestmark = pypg.require_injection_points()


def test_signal_autovacuum(create_pg):
    # autovacuum_naptime = 1 ensures a worker spawns quickly.
    node = create_pg("node", conf={"autovacuum_naptime": 1})
    node.sql("CREATE EXTENSION injection_points")

    node.sql_batch(
        "CREATE ROLE regress_regular_role",
        "CREATE ROLE regress_worker_role",
        "GRANT pg_signal_autovacuum_worker TO regress_worker_role",
    )

    # From this point, autovacuum workers wait at startup.
    node.sql(f"SELECT injection_points_attach('{POINT}', 'wait')")
    # Accelerate worker creation in case the naptime has not yet elapsed.
    node.pg_ctl("reload")

    node.wait_for_event("autovacuum worker", POINT)
    av_pid = node.sql(
        "SELECT pid FROM pg_stat_activity WHERE backend_type = 'autovacuum worker' "
        f"AND wait_event = '{POINT}' LIMIT 1"
    )

    # A regular role cannot terminate an autovacuum worker.
    with node.connect() as conn:
        conn.sql("SET ROLE regress_regular_role")
        with pytest.raises(
            LibpqError, match="permission denied to terminate process"
        ) as exc:
            conn.sql(f"SELECT pg_terminate_backend('{av_pid}')")
        assert exc.value.detail == (
            'Only roles with privileges of the "pg_signal_autovacuum_worker" '
            "role may terminate autovacuum workers."
        )

    offset = node.current_log_position()

    # A role with pg_signal_autovacuum_worker can terminate the worker.
    with node.connect() as conn:
        conn.sql("SET ROLE regress_worker_role")
        conn.sql(f"SELECT pg_terminate_backend('{av_pid}')")

    # Wait for the worker to exit before scanning the logs.
    node.poll_query_until(
        f"SELECT count(*) = 0 FROM pg_stat_activity WHERE pid = '{av_pid}' "
        "AND backend_type = 'autovacuum worker'"
    )

    node.wait_for_log(
        r"FATAL: .*terminating autovacuum process due to administrator command",
        offset,
    )

    node.sql(f"SELECT injection_points_detach('{POINT}')")
