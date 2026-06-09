# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/commit_ts/t/004_restart.pl.

Testing of commit timestamp preservation across restarts.
"""

import pytest

from libpq import LibpqError


def test_commit_ts_restart(create_pg):
    node = create_pg("committs_restart", conf={"track_commit_timestamp": True})

    with pytest.raises(
        LibpqError, match="cannot retrieve commit timestamp for transaction"
    ):
        node.sql("SELECT pg_xact_commit_timestamp('0')")

    # BootstrapTransactionId and FrozenTransactionId succeed but are null.
    assert node.sql("SELECT pg_xact_commit_timestamp('1')") is None
    assert node.sql("SELECT pg_xact_commit_timestamp('2')") is None
    # FirstNormalTransactionId occurred during initdb, before commit
    # timestamps were enabled, so it is null too.
    assert node.sql("SELECT pg_xact_commit_timestamp('3')") is None

    node.sql("CREATE TABLE committs_test(x integer, y timestamp with time zone)")

    node.sql("BEGIN")
    node.sql("INSERT INTO committs_test(x, y) VALUES (1, current_timestamp)")
    xid = node.sql("SELECT pg_current_xact_id()::xid")
    node.sql("COMMIT")

    before_restart_ts = node.sql(f"SELECT pg_xact_commit_timestamp('{xid}')")
    assert before_restart_ts not in ("", None)

    node.stop("immediate")
    node.start()
    assert node.sql(f"SELECT pg_xact_commit_timestamp('{xid}')") == before_restart_ts

    node.stop("fast")
    node.start()
    assert node.sql(f"SELECT pg_xact_commit_timestamp('{xid}')") == before_restart_ts

    # Now disable commit timestamps.
    node.append_conf(track_commit_timestamp=False)
    node.stop("fast")
    # Start once to emit XLOG_PARAMETER_CHANGE, then restart so that record is
    # not replayed by the follow-up immediate shutdown.
    node.start()
    node.pg_ctl("restart")

    # Move commit timestamps across page boundaries; things should still work
    # across restarts for transactions committed while tracking is disabled.
    node.sql(
        "CREATE PROCEDURE consume_xid(cnt int)\n"
        "AS $$\n"
        "DECLARE\n"
        "    i int;\n"
        "    BEGIN\n"
        "        FOR i in 1..cnt LOOP\n"
        "            EXECUTE 'SELECT pg_current_xact_id()';\n"
        "            COMMIT;\n"
        "        END LOOP;\n"
        "    END;\n"
        "$$\n"
        "LANGUAGE plpgsql;"
    )
    node.sql("CALL consume_xid(2000)")

    with pytest.raises(LibpqError, match="could not get commit timestamp data"):
        node.sql(f"SELECT pg_xact_commit_timestamp('{xid}')")

    # A transaction committed while commit timestamps are disabled.
    node.sql("BEGIN")
    node.sql("INSERT INTO committs_test(x, y) VALUES (2, current_timestamp)")
    xid_disabled = node.sql("SELECT pg_current_xact_id()")
    node.sql("COMMIT")

    with pytest.raises(LibpqError, match="could not get commit timestamp data"):
        node.sql(f"SELECT pg_xact_commit_timestamp('{xid_disabled}')")

    # Re-enable, restart with an immediate shutdown so recovery replays the
    # transactions committed while tracking was disabled.
    node.append_conf(track_commit_timestamp=True)
    node.stop("immediate")
    node.start()

    assert node.sql(f"SELECT pg_xact_commit_timestamp('{xid}')") is None
    assert node.sql(f"SELECT pg_xact_commit_timestamp('{xid_disabled}')") is None
