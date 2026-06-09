# Copyright (c) 2026, PostgreSQL Global Development Group

"""Port of src/test/modules/worker_spi/t/002_worker_terminate.pl.

Tests that background workers can be terminated by database commands: a
non-interruptible worker blocks CREATE DATABASE WITH TEMPLATE, while a
BGWORKER_INTERRUPTIBLE worker is terminated by such commands.
"""

import pytest

import pypg
from libpq import LibpqError


@pypg.require_injection_points()
def test_worker_terminate(create_pg):
    # A large naptime gives slow machines room to process the interrupt
    # requests sent by the database commands below.
    node = create_pg(
        "worker_terminate",
        conf={
            "autovacuum": False,
            "debug_parallel_query": False,
            "log_min_messages": "debug1",
            "worker_spi.naptime": 600,
        },
    )
    node.sql("CREATE EXTENSION worker_spi")

    def launch(database, testcase, interruptible):
        # Launch a worker_spi dynamic worker; wait until it is napping.
        pid = node.sql(
            f"SELECT worker_spi_launch({testcase}, '{database}'::regdatabase, 0, "
            f"'{{}}', {interruptible})"
        )
        node.poll_query_until(
            f"SELECT wait_event FROM pg_stat_activity WHERE pid = {pid}",
            expected="WorkerSpiMain",
        )
        return pid

    def run_interruptible(command, pid):
        # The command terminates the interruptible worker and then completes.
        offset = node.current_log_position()
        node.sql(command)
        node.wait_for_log(
            r'terminating background worker "worker_spi dynamic" '
            r"due to administrator command",
            offset,
        )
        node.wait_for_log(
            rf'background worker "worker_spi dynamic" \(PID {pid}\) '
            r"exited with exit code",
            offset,
        )
        assert (
            node.sql(f"SELECT count(*) = 0 FROM pg_stat_activity WHERE pid = {pid}")
            is True
        )

    # A non-interruptible worker blocks CREATE DATABASE WITH TEMPLATE. The
    # procarray-reduce-count injection point cuts the backend-retry count so
    # the command fails quickly (see CountOtherDBBackends()).
    launch("postgres", 0, "false")
    node.sql("CREATE EXTENSION injection_points")
    node.sql("SELECT injection_points_attach('procarray-reduce-count', 'error')")
    with pytest.raises(
        LibpqError, match='source database "postgres" is being accessed by other users'
    ):
        node.sql("CREATE DATABASE testdb WITH TEMPLATE postgres")
    assert (
        node.sql(
            "SELECT count(1) FROM pg_stat_activity "
            "WHERE backend_type = 'worker_spi dynamic'"
        )
        == 1
    )
    node.sql(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE backend_type = 'worker_spi dynamic'"
    )
    node.sql("SELECT injection_points_detach('procarray-reduce-count')")

    # BGWORKER_INTERRUPTIBLE workers are terminated by database commands.
    pid = launch("postgres", 1, "true")
    run_interruptible("CREATE DATABASE testdb WITH TEMPLATE postgres", pid)

    pid = launch("testdb", 2, "true")
    run_interruptible("ALTER DATABASE testdb RENAME TO renameddb", pid)

    # Put the tablespace directory under the data directory's parent rather
    # than pytest's tmp_path: on Windows the (privilege-dropped) postmaster
    # must be able to set permissions on it, and the CI grants the needed ACLs
    # on the test tree but not on the system temp directory.
    tablespace = node.datadir.parent / "test_tablespace"
    tablespace.mkdir()
    node.sql(f"CREATE TABLESPACE test_tablespace LOCATION '{tablespace}'")

    pid = launch("renameddb", 3, "true")
    run_interruptible("ALTER DATABASE renameddb SET TABLESPACE test_tablespace", pid)

    pid = launch("renameddb", 4, "true")
    run_interruptible("DROP DATABASE renameddb", pid)
