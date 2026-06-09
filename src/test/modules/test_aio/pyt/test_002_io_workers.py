# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/test_aio/t/002_io_workers.pl.

Tests changing the number of I/O worker processes at runtime, including the
handling of their termination.
"""

import pytest

from libpq import LibpqError

# The Perl test shuffles 1..32 and samples a few; a fixed sample is enough and
# keeps the run deterministic. Always include the min and max.
WORKER_COUNTS = [1, 32, 7, 19]


IO_WORKER_COUNT = (
    "SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'io worker'"
)


def test_io_workers_dynamic(create_pg):
    node = create_pg(
        "io_workers",
        conf={
            "io_method": "worker",
            "io_worker_idle_timeout": "0ms",
            "io_worker_launch_interval": "0ms",
            "io_max_workers": 32,
        },
    )

    # Out-of-range values are rejected.
    for bad in (0, 33):
        with pytest.raises(
            LibpqError,
            match=f'{bad} is outside the valid range for parameter "io_min_workers"',
        ):
            node.sql(f"ALTER SYSTEM SET io_min_workers = {bad}")

    for count in WORKER_COUNTS:
        node.sql(f"ALTER SYSTEM SET io_min_workers = {count}")
        node.sql("SELECT pg_reload_conf()")
        # A fresh connection so it picks up the reloaded GUC immediately,
        # rather than racing the SIGHUP against the cached session.
        assert node.sql_oneshot("SHOW io_min_workers") == str(count)

        # The pool reaches the requested size, ...
        node.poll_query_until(IO_WORKER_COUNT, expected=count)

        # ... a terminated worker is noticed and replaced.
        pid = node.sql(
            "SELECT pid FROM pg_stat_activity WHERE backend_type = 'io worker' "
            "ORDER BY random() LIMIT 1"
        )
        node.pg_ctl("kill", "INT", str(pid))
        node.poll_query_until(
            f"SELECT count(*) FROM pg_stat_activity WHERE pid = {pid}", expected=0
        )
        node.poll_query_until(IO_WORKER_COUNT, expected=count)

    node.stop()
