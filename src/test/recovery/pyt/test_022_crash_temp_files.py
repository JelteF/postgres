# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/022_crash_temp_files.pl.

Tests removal of temporary files after a crash. A backend's batch INSERT blocks
on a UNIQUE-constraint lock held by a second open transaction while it has a
temporary file open; that backend is then SIGKILLed to force a crash-restart.
With ``remove_temp_files_after_crash = on`` the stranded temp file is gone once
the server is back; with it ``off`` the file survives the crash-restart and is
only removed by a clean restart.
"""

import pytest

from libpq import LibpqError
from pypg._env import test_timeout_default
from pypg.util import wait_until

# A session whose backend (or whose peer backend) was killed by a crash fails
# with one of these libpq/server messages.
DIED = (
    "terminating connection because of crash of another server process"
    "|server closed the connection unexpectedly"
    "|connection to server was lost"
    "|could not send data to server"
    "|no connection to the server"
)

TEMP_FILE_COUNT = "SELECT COUNT(1) FROM pg_ls_dir('base/pgsql_tmp')"


def wait_for_reconnect(node):
    """Poll, reconnecting each attempt, until the server accepts queries again.

    Uses a fresh one-shot connection each attempt, since the cached sql()
    connection is the one pointing at the crashed backend. Once the server is
    back, also drop that stale cached connection so the caller's subsequent
    node.sql() calls open a fresh one instead of hitting the same dead socket.
    """
    for _ in wait_until("server did not come back after crash", timeout=180):
        try:
            if node.sql_oneshot("SELECT 1") == 1:
                node.close_default_conn()
                return
        except LibpqError:
            pass


def test_crash_temp_files(create_pg):
    node = create_pg("node_crash")

    # Small work_mem so a modest INSERT spills to a temporary file; enable
    # crash-restart and removal of temp files after a crash.
    node.sql("ALTER SYSTEM SET remove_temp_files_after_crash = on")
    node.sql("ALTER SYSTEM SET log_connections = receipt")
    node.sql("ALTER SYSTEM SET work_mem = '64kB'")
    node.sql("ALTER SYSTEM SET restart_after_crash = on")
    node.sql("SELECT pg_reload_conf()")

    node.sql("CREATE TABLE tab_crash (a integer UNIQUE)")

    def crash_with_temp_file():
        killme = node.connect()
        killme2 = node.connect()

        pid = killme.sql("SELECT pg_backend_pid()")

        # killme2 holds a lock on value 1 in an open transaction, so killme's
        # batch insert (which also inserts value 1) blocks with a temp file open.
        killme2.sql("BEGIN")
        killme2.sql("INSERT INTO tab_crash (a) VALUES(1)")

        killme.sql("BEGIN")
        killme_insert = killme.background_sql(
            "INSERT INTO tab_crash (a) SELECT i FROM generate_series(1, 5000) s(i)"
        )
        # Give this wait a fresh budget: under heavy parallel load the shared
        # per-test deadline can be nearly spent, and timing out here while the
        # asql above is still in flight crashes the worker at teardown.
        node.poll_query_until(
            f"SELECT count(*) > 0 FROM pg_locks WHERE pid = {pid} AND NOT granted",
            expected=True,
            timeout=test_timeout_default(),
        )

        node.pg_ctl("kill", "KILL", str(pid))

        with pytest.raises(LibpqError, match=DIED):
            killme_insert.result()
        # Wait for killme2 to report failure too, which also confirms the
        # postmaster has noticed its dead child and begun the restart cycle. A
        # plain "SELECT 1" would race the postmaster: if killme2's backend has
        # not been terminated yet the query just succeeds. Like the Perl test,
        # run a sleep that gets interrupted when the backend is terminated.
        with pytest.raises(LibpqError, match=DIED):
            killme2.sql(f"SELECT pg_sleep({test_timeout_default()})")

        wait_for_reconnect(node)

    crash_with_temp_file()
    assert node.sql(TEMP_FILE_COUNT) == 0, "no temporary files"

    # Old behavior: keep temporary files after a crash.
    node.sql("ALTER SYSTEM SET remove_temp_files_after_crash = off")
    node.sql("SELECT pg_reload_conf()")

    crash_with_temp_file()
    assert node.sql(TEMP_FILE_COUNT) == 1, "one temporary file"

    # A clean restart removes the temporary files.
    node.pg_ctl("restart")
    assert node.sql(TEMP_FILE_COUNT) == 0, "temporary file was removed"

    node.stop()
