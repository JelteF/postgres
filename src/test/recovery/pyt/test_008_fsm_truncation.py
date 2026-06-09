# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/008_fsm_truncation.pl.

Tests an FSM-driven INSERT right after a truncation has cleared the FSM slots
that recorded free space in the removed blocks. The FSM must not hand out a
page that no longer exists: after the standby is promoted and restarted (so its
in-memory FSM is discarded), an INSERT into the truncated relation must
succeed.
"""

from pypg._env import test_timeout_default


def test_fsm_truncation(create_pg):
    primary = create_pg(
        "primary",
        allows_streaming=True,
        conf={"max_prepared_transactions": 5, "autovacuum": False},
    )
    backup = primary.backup("primary_backup")
    standby = create_pg("standby", from_backup=backup, streaming_primary=primary)

    primary.sql("create table testtab (a int, b char(100))")
    primary.sql("insert into testtab select generate_series(1,1000), 'foo'")
    primary.sql("insert into testtab select generate_series(1,1000), 'foo'")
    primary.sql("delete from testtab where ctid > '(8,0)'")

    # Hold a lock via a prepared transaction so the following vacuum updates the
    # FSM but does not truncate the relation.
    primary.sql_batch(
        "begin", "lock table testtab in row share mode", "prepare transaction 'p1'"
    )
    primary.sql("vacuum verbose testtab")
    primary.sql("checkpoint")

    # More churn plus a vacuum, to force full-page writes.
    primary.sql("insert into testtab select generate_series(1,1000), 'foo'")
    primary.sql("delete from testtab where ctid > '(8,0)'")
    primary.sql("vacuum verbose testtab")

    # Make all buffers clean on the standby.
    standby.sql("checkpoint")

    # Release the lock and vacuum again, which now truncates the relation.
    primary.sql("rollback prepared 'p1'")
    primary.sql("vacuum verbose testtab")
    primary.sql("checkpoint")

    until_lsn = primary.lsn("write")
    # The two vacuums above each spend ~5s waiting for the truncation lock, so
    # the per-test deadline may already be spent; give the catchup wait a fresh
    # budget the way Perl gives each poll_query_until its own timeout_default.
    standby.poll_query_until(
        f"SELECT '{until_lsn}'::pg_lsn <= pg_last_wal_replay_lsn()",
        timeout=test_timeout_default(),
    )

    standby.promote()
    standby.sql("checkpoint")

    # Restart to discard the in-memory copy of the FSM.
    standby.pg_ctl("restart")

    # INSERT must succeed against the truncated relation's FSM.
    standby.sql("insert into testtab select generate_series(1,1000), 'foo'")
