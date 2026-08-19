# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/012_subtransactions.pl.

Subtransaction-focused recovery tests: that replay correctly populates SUBTRANS
and advances nextXid so it cannot collide with replayed savepoint XIDs, and
that a 2PC transaction with more than PGPROC_MAX_CACHED_SUBXIDS subtransactions
survives a promotion with its data visibility intact (committed visible,
prepared not visible until the 2PC is finished). The promoted standby becomes
the new primary and the old primary is re-attached as a standby (a role swap)
between rounds.
"""

# The recursive function from src/test/regress/sql/hs_primary_extremes.sql:
# inserts n, n-1, ..., 1 each in its own subtransaction (exception block).
HS_SUBXIDS = """
    CREATE OR REPLACE FUNCTION hs_subxids (n integer)
    RETURNS void
    LANGUAGE plpgsql
    AS $$
    BEGIN
        IF n <= 0 THEN RETURN; END IF;
        INSERT INTO t_012_tbl VALUES (n);
        PERFORM hs_subxids(n - 1);
        RETURN;
    EXCEPTION WHEN raise_exception THEN NULL; END;
    $$;
"""

SUM_QUERY = "SELECT coalesce(sum(id),-1) FROM t_012_tbl"


def test_subtransactions(create_pg):
    primary = create_pg(
        "primary",
        allows_streaming=True,
        conf={"max_prepared_transactions": 10, "log_checkpoints": True},
    )
    backup = primary.backup("primary_backup")
    primary.sql("CREATE TABLE t_012_tbl (id int)")

    standby = create_pg("standby", from_backup=backup, streaming_primary=primary)

    # Switch to synchronous replication so commits are durable on the standby
    # before a promotion.
    primary.append_conf(synchronous_standby_names="*")
    primary.sql("SELECT pg_reload_conf()")

    # Replay must set SUBTRANS and advance nextXid past the prepared savepoint
    # XIDs so a later transaction does not reuse them.
    primary.sql_batch(
        "BEGIN",
        "DELETE FROM t_012_tbl",
        "INSERT INTO t_012_tbl VALUES (43)",
        "SAVEPOINT s1",
        "INSERT INTO t_012_tbl VALUES (43)",
        "SAVEPOINT s2",
        "INSERT INTO t_012_tbl VALUES (43)",
        "SAVEPOINT s3",
        "INSERT INTO t_012_tbl VALUES (43)",
        "SAVEPOINT s4",
        "INSERT INTO t_012_tbl VALUES (43)",
        "SAVEPOINT s5",
        "INSERT INTO t_012_tbl VALUES (43)",
        "PREPARE TRANSACTION 'xact_012_1'",
    )
    primary.sql("CHECKPOINT")

    primary.pg_ctl("restart")
    # Would reuse a savepoint XID here if nextXid was not advanced on replay.
    primary.sql_batch("BEGIN", "INSERT INTO t_012_tbl VALUES (142)", "ROLLBACK")
    primary.sql("COMMIT PREPARED 'xact_012_1'")
    assert primary.sql("SELECT count(*) FROM t_012_tbl") == 6, (
        "Check nextXid handling for prepared subtransactions"
    )

    # A committed transaction with >PGPROC_MAX_CACHED_SUBXIDS subxacts stays
    # visible across a promotion.
    primary.sql("DELETE FROM t_012_tbl")
    primary.sql(HS_SUBXIDS)
    primary.sql_batch("BEGIN", "SELECT hs_subxids(127)", "COMMIT")
    primary.wait_for_catchup(standby)
    assert standby.sql(SUM_QUERY) == 8128, "Visible"
    primary.stop()
    standby.promote()
    assert standby.sql(SUM_QUERY) == 8128, "Visible"

    # Role swap: the promoted standby is the new primary; re-attach the old one.
    primary, standby = standby, primary
    standby.enable_streaming(primary)
    standby.start()
    assert standby.sql(SUM_QUERY) == 8128, "Visible"

    # A prepared (not committed) transaction with >PGPROC_MAX_CACHED_SUBXIDS
    # subxacts is not visible, and stays so across a promotion until finished.
    primary.sql("DELETE FROM t_012_tbl")
    primary.sql(HS_SUBXIDS)
    primary.sql_batch(
        "BEGIN", "SELECT hs_subxids(127)", "PREPARE TRANSACTION 'xact_012_1'"
    )
    primary.wait_for_catchup(standby)
    assert standby.sql(SUM_QUERY) == -1, "Not visible"
    primary.stop()
    standby.promote()
    assert standby.sql(SUM_QUERY) == -1, "Not visible"

    primary, standby = standby, primary
    standby.enable_streaming(primary)
    standby.start()
    primary.sql("COMMIT PREPARED 'xact_012_1'")  # must succeed on promoted standby
    assert primary.sql(SUM_QUERY) == 8128, "Visible"

    # Same again, but with even more subxacts and a rollback at the end.
    primary.sql("DELETE FROM t_012_tbl")
    primary.sql_batch(
        "BEGIN", "SELECT hs_subxids(201)", "PREPARE TRANSACTION 'xact_012_1'"
    )
    primary.wait_for_catchup(standby)
    assert standby.sql(SUM_QUERY) == -1, "Not visible"
    primary.stop()
    standby.promote()
    assert standby.sql(SUM_QUERY) == -1, "Not visible"

    primary, standby = standby, primary
    standby.enable_streaming(primary)
    standby.start()
    primary.sql("ROLLBACK PREPARED 'xact_012_1'")  # must succeed on promoted standby
    assert primary.sql(SUM_QUERY) == -1, "Not visible"
