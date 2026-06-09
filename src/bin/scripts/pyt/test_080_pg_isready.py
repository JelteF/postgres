# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/scripts/t/080_pg_isready.pl."""

from pypg._env import test_timeout_default


def test_standard_options(pg_bin):
    pg_bin.check_help("pg_isready")
    pg_bin.check_version("pg_isready")
    pg_bin.check_bad_option("pg_isready")


def test_fails_with_no_server_running(create_pg, pg_bin):
    node = create_pg("isready_down")
    node.stop()
    assert pg_bin.run("pg_isready", server=node).returncode != 0


def test_succeeds_with_server_running(node, pg_bin):
    r = pg_bin.run(
        "pg_isready", "--timeout", str(int(test_timeout_default())), server=node
    )
    assert r.returncode == 0, r.stderr
