# Copyright (c) 2026, PostgreSQL Global Development Group

"""Port of src/test/modules/test_checksums/t/001_basic.pl.

Enable and disable data checksums in an online cluster.
"""

_STATE = "SELECT setting FROM pg_catalog.pg_settings WHERE name = 'data_checksums'"
_LAUNCHER_GONE = (
    "SELECT count(*) = 0 FROM pg_catalog.pg_stat_activity"
    " WHERE backend_type = 'datachecksums launcher'"
)


def _enable(node, wait=None, cost_delay=0, cost_limit=100):
    node.sql(f"SELECT pg_enable_data_checksums({cost_delay}, {cost_limit})")
    if wait is not None:
        node.poll_query_until(_STATE, expected=wait)
        if wait in ("on", "off"):
            node.poll_query_until(_LAUNCHER_GONE, expected=True)


def _disable(node, wait=False):
    node.sql("SELECT pg_disable_data_checksums()")
    if wait:
        node.poll_query_until(_STATE, expected="off")
        node.poll_query_until(_LAUNCHER_GONE, expected=True)


def test_online_checksums(create_pg):
    # Initialize with checksums disabled (the template has them enabled).
    node = create_pg("checksums_basic", initdb_opts=["--no-data-checksums"])

    # Create some un-checksummed data.
    node.sql("CREATE TABLE t AS SELECT generate_series(1,10000) AS a;")
    assert node.sql(_STATE) == "off"

    # Enable data checksums and wait for the 'on' state transition.
    _enable(node, wait="on")
    assert node.sql("SELECT count(*) FROM t WHERE a > 1") == 9999

    # Enabling again is a no-op, so don't wait for any transition.
    _enable(node)
    assert node.sql(_STATE) == "on"
    node.sql("UPDATE t SET a = a + 1;")
    assert node.sql("SELECT count(*) FROM t WHERE a > 1") == 10000

    # Disable checksums and wait for the transition.
    _disable(node, wait=True)
    assert node.sql("SELECT count(*) FROM t WHERE a > 1") == 10000

    # Re-enable after changing the data so the checksums differ.
    node.sql("UPDATE t SET a = a + 1;")
    _enable(node, wait="on")
    assert node.sql("SELECT count(*) FROM t WHERE a > 1") == 10000

    node.stop()
