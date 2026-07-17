# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/scripts/t/080_pg_isready.pl.

pg_isready reports whether a server is accepting connections, so the cases
split by reachability: against a running server it must succeed, and against a
server that was never started it must fail.
"""

from pypg.bins import pg_isready
from pypg._env import test_timeout_default


def test_help_version_options():
    pg_isready.check_standard_options()


def test_no_server_running(create_pg):
    # Use a dedicated, never-started node rather than the shared module server:
    # this case needs a server that is *not* accepting connections, and stopping
    # the shared `pg` server would break the isolation other modules rely on.
    # start=False gives an initialized data directory with no running postmaster.
    node = create_pg("main", start=False)
    pg_isready.check_all(server=node, exit_code=2)


def test_server_running(pg):
    # The shared module server is up, so pg_isready against it should succeed.
    pg_isready("--timeout", str(test_timeout_default()), server=pg)
