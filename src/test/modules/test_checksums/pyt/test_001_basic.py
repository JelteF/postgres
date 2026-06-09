# Copyright (c) 2026, PostgreSQL Global Development Group

"""Port of src/test/modules/test_checksums/t/001_basic.pl.

Enable and disable data checksums in an online cluster.
"""

import pytest

from libpq import LibpqError
from pypg.bins import pg_checksums

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


def _wait_for_temp_table_wait(node, dbname):
    """Wait until the checksums worker for ``dbname`` is parked waiting for
    pre-existing temporary tables to disappear."""
    node.poll_query_until(
        "SELECT count(*) > 0 FROM pg_stat_activity"
        " WHERE backend_type = 'datachecksums worker' AND datname = $1"
        " AND query LIKE 'Waiting for % temp tables to be removed'",
        dbname,
    )


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

    # Enabling checksums in a cluster which contains an invalid database left
    # behind by an interrupted DROP DATABASE must be refused.
    _disable(node, wait=True)

    node.sql("CREATE DATABASE baddb")
    node.sql_oneshot(
        "CREATE TABLE bad_t AS SELECT generate_series(1,100) AS a", dbname="baddb"
    )

    # Mark the database invalid, as an interrupted DROP DATABASE would.
    node.sql("UPDATE pg_database SET datconnlimit = -2 WHERE datname = 'baddb'")

    # The request must fail up front with an actionable error, rather than fail
    # halfway through processing. The message both names the offending database
    # and hints at how to get rid of it.
    with pytest.raises(LibpqError, match='invalid database "baddb"') as excinfo:
        node.sql("SELECT pg_enable_data_checksums()")
    assert "DROP DATABASE" in excinfo.value.hint
    assert node.sql(_STATE) == "off"

    # Dropping the invalid database clears the way.
    node.sql("DROP DATABASE baddb")
    _enable(node, wait="on")

    # A database dropped while processing is in progress is not an error, the
    # remaining databases are still processed.
    _disable(node, wait=True)

    node.sql("CREATE DATABASE dropme")
    node.sql_oneshot(
        "CREATE TABLE dropme_t AS SELECT generate_series(1,10000) AS a",
        dbname="dropme",
    )

    # Hold the worker in the "postgres" database by keeping a temporary table
    # around, the worker waits for pre-existing temp tables to disappear before
    # it reports the database as processed. "dropme" was created last, so it is
    # processed after "postgres" and is still untouched while we wait.
    with node.connect() as holder:
        holder.sql("CREATE TEMP TABLE holdme (a int)")

        _enable(node)
        _wait_for_temp_table_wait(node, "postgres")

        # Verify the assumption that processing has not reached "dropme" yet,
        # without it the test would silently stop covering the concurrent drop.
        assert (
            'initiating data checksum processing in database "dropme"'
            not in node.log_content()
        )

        # Not processed yet and nobody is connected to it, so this must succeed.
        node.sql("DROP DATABASE dropme")

        # Let the worker in "postgres" finish, the launcher then moves on to the
        # database which no longer exists.
        holder.sql("DROP TABLE holdme")

    node.poll_query_until(_STATE, expected="on")
    node.poll_query_until(_LAUNCHER_GONE, expected=True)

    # Same thing with DROP DATABASE ... WITH (FORCE), which terminates the
    # checksums worker connected to the database being dropped.
    _disable(node, wait=True)

    node.sql("CREATE DATABASE dropmeforce")
    node.sql_oneshot(
        "CREATE TABLE dropme_t AS SELECT generate_series(1,10000) AS a",
        dbname="dropmeforce",
    )

    # Hold the worker inside "dropmeforce" by keeping a temporary table around
    # there.
    with node.connect(dbname="dropmeforce") as holder:
        holder.sql("CREATE TEMP TABLE holdme (a int)")

        _enable(node)
        _wait_for_temp_table_wait(node, "dropmeforce")

        # Terminates both the session holding the temp table and the checksums
        # worker connected to the database. The holder connection is dead
        # afterwards, so it is only closed by leaving this block.
        node.sql("DROP DATABASE dropmeforce WITH (FORCE)")

    node.poll_query_until(_STATE, expected="on")
    node.poll_query_until(_LAUNCHER_GONE, expected=True)

    assert node.sql("SELECT count(*) FROM t WHERE a > 1") == 10000

    node.stop()

    # The resulting cluster must also pass offline verification, proving no
    # unchecksummed files were left behind.
    pg_checksums("--check", "-D", node.datadir)
