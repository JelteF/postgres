# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/015_promotion_pages.pl.

Tests promotion handling for WAL generated after promotion but before the
first post-recovery checkpoint. After a crash of the freshly-promoted node,
replay must not see invalid page references caused by a stale minimum
consistent recovery point, so the table's data must survive the crash intact.
"""


def test_promotion_pages(create_pg):
    # wal_log_hints = off is important to produce invalid page references.
    alpha = create_pg("alpha", allows_streaming=True, conf={"wal_log_hints": False})
    backup = alpha.backup("bkp")
    bravo = create_pg(
        "bravo",
        from_backup=backup,
        streaming_primary=alpha,
        conf={"checkpoint_timeout": "1h"},
    )

    alpha.sql("create table test1 (a int)")
    alpha.sql("insert into test1 select generate_series(1, 10000)")
    alpha.sql("checkpoint")

    # This vacuum sets visibility-map bits, creating the problematic WAL.
    alpha.sql("vacuum verbose test1")
    alpha.wait_for_catchup(bravo)

    # Force a checkpoint on the standby so its redo does not start from an older
    # point that would include the create table and initial page additions.
    bravo.sql("checkpoint")

    # Move minRecoveryPoint beyond the vacuum with some unrelated activity.
    alpha.sql("create table test2 (a int, b bytea)")
    alpha.sql(
        "insert into test2 select generate_series(1,10000), "
        "sha256(random()::text::bytea)"
    )
    alpha.sql("truncate test2")
    alpha.wait_for_catchup(bravo)

    # Promotion reinitializes minRecoveryPoint so WAL is replayed to the end.
    bravo.promote()

    # Create new page references before the first post-recovery checkpoint.
    bravo.sql("truncate test1")
    bravo.sql("vacuum verbose test1")
    bravo.sql("insert into test1 select generate_series(1,1000)")

    # Crash and restart: replay must not see invalid page references.
    bravo.stop("immediate")
    bravo.start()

    assert bravo.sql("SELECT count(*) FROM test1") == 1000, (
        "Check that table state is correct"
    )
