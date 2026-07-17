# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of contrib/pg_prewarm/t/001_basic.pl."""

import re

import pytest

from libpq import LibpqError
from pypg.bins import pg_controldata


def test_pg_prewarm(create_pg):
    # pg_prewarm must be preloaded (set at server start via conf).
    node = create_pg(
        "main",
        conf={
            "shared_preload_libraries": "pg_prewarm",
            "pg_prewarm.autoprewarm": True,
            "pg_prewarm.autoprewarm_interval": 0,
        },
    )
    # Let the unprivileged test_user connect. Prepend a local trust rule (Unix
    # peer auth would otherwise reject it) ahead of -- not replacing -- the
    # existing rules, so the default connections (incl. Windows TCP host trust)
    # keep working.
    hba = node.datadir / "pg_hba.conf"
    hba.write_text("local all test_user trust\n" + hba.read_text())
    node.pg_ctl("reload")

    node.sql_batch(
        "CREATE EXTENSION pg_prewarm",
        "CREATE TABLE test(c1 int)",
        "INSERT INTO test SELECT generate_series(1, 100)",
        "CREATE INDEX test_idx ON test(c1)",
        "CREATE ROLE test_user LOGIN",
    )

    assert node.sql("SELECT pg_prewarm('test', 'read');") >= 1
    assert node.sql("SELECT pg_prewarm('test', 'buffer');") >= 1

    # prefetch mode might or might not be available in this build.
    try:
        assert node.sql("SELECT pg_prewarm('test', 'prefetch');") >= 1
    except LibpqError as e:
        assert "prefetch is not supported by this build" in str(e)

    # test_user lacks privileges to prewarm the table/index.
    test_user = node.connect(user="test_user")
    with pytest.raises(LibpqError, match="permission denied for table test"):
        test_user.sql("SELECT pg_prewarm('test');")
    with pytest.raises(LibpqError, match="permission denied for index test_idx"):
        test_user.sql("SELECT pg_prewarm('test_idx');")

    # With privileges, test_user can prewarm the table and its index.
    node.sql("GRANT SELECT ON test TO test_user;")
    assert test_user.sql("SELECT pg_prewarm('test');") >= 1
    assert test_user.sql("SELECT pg_prewarm('test_idx');") >= 1

    assert node.sql("SELECT autoprewarm_dump_now();") >= 1

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
    out = pg_controldata.capture(node.datadir)
    assert re.search(r"Database cluster state:\s*shut down", out)
