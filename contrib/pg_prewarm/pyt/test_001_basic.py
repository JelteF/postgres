# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of contrib/pg_prewarm/t/001_basic.pl."""

import re

import pytest

from libpq import LibpqError


def test_pg_prewarm(create_pg, pg_bin):
    node = create_pg("main")
    with node.restarting() as s:
        s.conf.set(
            **{
                "shared_preload_libraries": "pg_prewarm",
                "pg_prewarm.autoprewarm": "true",
                "pg_prewarm.autoprewarm_interval": 0,
            }
        )
        s.hba.prepend(["local", "all", "test_user", "trust"])

    conn = node.connect()
    conn.sql(
        "CREATE EXTENSION pg_prewarm;"
        "CREATE TABLE test(c1 int);"
        "INSERT INTO test SELECT generate_series(1, 100);"
        "CREATE INDEX test_idx ON test(c1);"
        "CREATE ROLE test_user LOGIN;"
    )

    assert conn.sql("SELECT pg_prewarm('test', 'read');") >= 1
    assert conn.sql("SELECT pg_prewarm('test', 'buffer');") >= 1

    # prefetch mode might or might not be available in this build.
    try:
        assert conn.sql("SELECT pg_prewarm('test', 'prefetch');") >= 1
    except LibpqError as e:
        assert "prefetch is not supported by this build" in str(e)

    # test_user lacks privileges to prewarm the table/index.
    test_user = node.connect(user="test_user")
    with pytest.raises(LibpqError, match="permission denied for table test"):
        test_user.sql("SELECT pg_prewarm('test');")
    with pytest.raises(LibpqError, match="permission denied for index test_idx"):
        test_user.sql("SELECT pg_prewarm('test_idx');")

    # With privileges, test_user can prewarm the table and its index.
    conn.sql("GRANT SELECT ON test TO test_user;")
    assert test_user.sql("SELECT pg_prewarm('test');") >= 1
    assert test_user.sql("SELECT pg_prewarm('test_idx');") >= 1

    assert conn.sql("SELECT autoprewarm_dump_now();") >= 1

    # Restart, to verify that autoprewarm actually works.
    offset = node.current_log_position()
    node.pg_ctl("restart")
    node.wait_for_log(
        r"autoprewarm successfully prewarmed [1-9][0-9]* of [0-9]+"
        r" previously-loaded blocks",
        offset,
    )

    node.stop()

    # The control file should indicate a normal shutdown.
    out = pg_bin.run("pg_controldata", node.datadir).stdout
    assert re.search(r"Database cluster state:\s*shut down", out)
