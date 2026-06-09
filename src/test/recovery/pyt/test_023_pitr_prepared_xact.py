# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/023_pitr_prepared_xact.pl.

Tests point-in-time recovery with a prepared transaction: recovery targets a
named restore point issued just after a PREPARE TRANSACTION, so the promoted
node still has the 2PC transaction pending and an explicit COMMIT PREPARED is
needed. An INSERT done after the restore point must not appear.
"""


def test_pitr_prepared_xact(create_pg):
    primary = create_pg(
        "primary", archiving=True, allows_streaming=True,
        conf=["max_prepared_transactions = 10"],
    )
    backup = primary.backup("my_backup")

    # PITR node targeting the restore point 'rp', promoting when reached. Built
    # with start=False so the primary workload and restore point exist first.
    node_pitr = create_pg(
        "node_pitr",
        from_backup=backup,
        restoring=primary,
        restoring_standby=False,
        start=False,
        conf=[
            "recovery_target_name = 'rp'",
            "recovery_target_action = 'promote'",
        ],
    )

    # Workload: prepare a transaction, mark the restore point, then insert more
    # (which must be recovered away).
    pconn = primary.connect()
    pconn.sql("CREATE TABLE foo(i int)")
    pconn.sql("BEGIN; INSERT INTO foo VALUES(1); PREPARE TRANSACTION 'fooinsert'")
    pconn.sql("SELECT pg_create_restore_point('rp')")
    pconn.sql("INSERT INTO foo VALUES(2)")

    walfile = pconn.sql("SELECT pg_walfile_name(pg_current_wal_lsn())")
    pconn.sql("SELECT pg_switch_wal()")
    primary.poll_query_until(
        f"SELECT '{walfile}' <= last_archived_wal FROM pg_stat_archiver"
    )

    node_pitr.start()
    node_pitr.poll_query_until("SELECT pg_is_in_recovery() = 'f'")
    pitr_conn = node_pitr.connect()

    # Commit the prepared transaction on the new timeline. Only its row should
    # be present; the INSERT after the restore point was recovered away.
    pitr_conn.sql("COMMIT PREPARED 'fooinsert'")
    assert pitr_conn.sql("SELECT * FROM foo") == 1, (
        "check table contents after COMMIT PREPARED"
    )

    # More data + a checkpoint on the post-promotion timeline, then crash and
    # restart to confirm the checkpoint record is found.
    pitr_conn.sql("INSERT INTO foo VALUES(3)")
    pitr_conn.sql("CHECKPOINT")
    node_pitr.stop("immediate")
    node_pitr.start()
