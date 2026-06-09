# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/048_vacuum_horizon_floor.pl.

Test that vacuum prunes away all dead tuples killed before OldestXmin.

This creates a table on a primary, updates it to generate dead tuples, and then
during the vacuum uses the replica to force the primary's
GlobalVisState->maybe_needed to move backwards so it precedes the OldestXmin
established at the start of the table vacuum. Before the fix, pruning would find
the final dead tuple HEAPTUPLE_RECENTLY_DEAD (its xmax follows the new
maybe_needed) while freezing decided its xmax precedes OldestXmin, erroring out
in heap_pre_freeze_checks() with "cannot freeze committed xmax".
"""

TABLE = "vac_horizon_floor_table"
CURSOR = "vac_horizon_floor_cursor1"
TEST_DB = "test_db"

# Enough rows to exceed maintenance_work_mem (set to its minimum) on all
# supported platforms, forcing two rounds of index vacuuming, while keeping the
# runtime short.
NROWS = 2000


def _wal_receiver_present(node, present):
    node.poll_query_until(
        "SELECT EXISTS (SELECT * FROM pg_stat_wal_receiver)",
        expected=present,
        dbname=TEST_DB,
    )


def test_vacuum_horizon_floor(create_pg):
    primary = create_pg(
        "primary",
        allows_streaming=True,
        conf={
            "hot_standby_feedback": True,
            "autovacuum": False,
            "log_min_messages": "INFO",
            "maintenance_work_mem": 64,
            # io_combine_limit = 1 avoids pinning more than one buffer at a time,
            # for test determinism.
            "io_combine_limit": 1,
        },
    )
    backup = primary.backup("my_backup")
    replica = create_pg("standby", from_backup=backup, streaming_primary=primary)

    primary.sql(f"CREATE DATABASE {TEST_DB}")

    # Save the original connection info for later use. Include the standby's
    # application_name (its node name) so that after the test resets
    # primary_conninfo to this value the walreceiver reconnects under that name;
    # otherwise it would default to "walreceiver" and wait_for_catchup(), which
    # matches on application_name, would never find the standby in
    # pg_stat_replication. The framework's streaming_primary= sets the same
    # application_name on the initial primary_conninfo.
    orig_conninfo = primary.connstr(application_name=replica.name)

    # Two long-running primary sessions.
    session_a = primary.connect(dbname=TEST_DB)
    session_b = primary.connect(dbname=TEST_DB)

    # Fill the table so a vacuum needs two rounds of index vacuuming: the first
    # round updates the backend's GlobalVisState (via
    # _bt_pendingfsm_finalize -> GetOldestNonRemovableTransactionId), so a later
    # tuple visibility check happens after maybe_needed has moved backwards.
    primary.sql_batch_oneshot(
        f"CREATE TABLE {TABLE}(col1 int) WITH (autovacuum_enabled=false, fillfactor=10)",
        f"INSERT INTO {TABLE} VALUES(7)",
        f"INSERT INTO {TABLE} SELECT generate_series(1, {NROWS}) % 3",
        f"CREATE INDEX on {TABLE}(col1)",
        f"DELETE FROM {TABLE} WHERE col1 = 0",
        f"INSERT INTO {TABLE} VALUES(7)",
        dbname=TEST_DB,
    )

    # We will later move the primary forward while the standby is disconnected;
    # for now wait for the standby to catch up.
    primary.wait_for_catchup(replica, "replay", primary.lsn("flush"))
    _wal_receiver_present(replica, True)

    # Set primary_conninfo to something invalid and reload. The startup process
    # forces the WAL receiver to restart and it can't reconnect.
    with replica.connect(dbname=TEST_DB) as conn:
        conn.sql("ALTER SYSTEM SET primary_conninfo = ''")
        conn.sql("SELECT pg_reload_conf()")
    _wal_receiver_present(replica, False)

    # Insert and update a tuple visible to the primary's vacuum but with xmax
    # newer than the oldest xmin on the recently-disconnected standby.
    res = session_a.sql_batch(
        f"INSERT INTO {TABLE} VALUES (99)",
        f"UPDATE {TABLE} SET col1 = 100 WHERE col1 = 99",
        "SELECT 'after_update'",
    )[-1]
    assert res == "after_update", "UPDATE occurred on primary session A"

    # Open a cursor whose pin keeps VACUUM from getting a cleanup lock on the
    # first page. VACUUM can start and compute OldestXmin/GlobalVisState but
    # then blocks, letting us reconnect the standby to push the horizon back
    # before pruning starts. The first inserted value was 7, so FETCH returns 7,
    # confirming the cursor holds a heap-page pin (index scans disabled).
    res = session_b.sql_batch(
        "BEGIN",
        "SET enable_bitmapscan = off",
        "SET enable_indexscan = off",
        "SET enable_indexonlyscan = off",
        f"DECLARE {CURSOR} CURSOR FOR SELECT * FROM {TABLE} WHERE col1 = 7",
        f"FETCH {CURSOR}",
    )[-1]
    assert res == 7, "Cursor query returned 7"

    vacuum_pid = session_a.sql("SELECT pg_backend_pid()")

    # Start a VACUUM FREEZE. It computes OldestXmin/GlobalVisState newer than all
    # dead tuples, then blocks on the cleanup lock held by the cursor's pin.
    # FREEZE makes it wait for the lock rather than skip the pinned page.
    session_a.sql("SET maintenance_io_concurrency = 0")
    vacuum = session_a.background_sql(f"VACUUM (VERBOSE, FREEZE, PARALLEL 0) {TABLE}")

    try:
        # Wait until VACUUM has computed its cutoffs and is just waiting on the
        # cleanup lock, before reconnecting the standby.
        primary.poll_query_until(
            f"SELECT count(*) >= 1 FROM pg_stat_activity "
            f"WHERE pid = {vacuum_pid} AND wait_event = 'BufferCleanup'",
            dbname=TEST_DB,
        )

        _wal_receiver_present(replica, False)

        # Allow the WAL receiver to re-establish, pushing the horizon backward.
        with replica.connect(dbname=TEST_DB) as conn:
            conn.sql(f"ALTER SYSTEM SET primary_conninfo = '{orig_conninfo}'")
            conn.sql("SELECT pg_reload_conf()")
        _wal_receiver_present(replica, True)

        # Once the WAL sender shows up, the standby has connected and pushed the
        # horizon back; session A won't see that until VACUUM does its first
        # round of index vacuuming.
        primary.poll_query_until(
            "SELECT EXISTS (SELECT * FROM pg_stat_replication)", dbname=TEST_DB
        )

        # Move the cursor to the next 7 (inserted much later), letting vacuum
        # proceed through most pages. With maintenance_work_mem minimal a round
        # of index vacuuming has happened and vacuum now waits on the cursor's
        # pin on the last page.
        res = session_b.sql(f"FETCH {CURSOR}")
        assert res == 7, "Cursor query returned 7 from second fetch"

        # Confirm a pass of index vacuuming actually happened.
        primary.poll_query_until(
            "SELECT index_vacuum_count > 0 FROM pg_stat_progress_vacuum "
            f"WHERE datname='{TEST_DB}' AND relid::regclass = '{TABLE}'::regclass",
            dbname=TEST_DB,
        )

        # Commit so VACUUM can finish. With the fix, pruning treats the final
        # dead tuple (xmax preceding OldestXmin) as HEAPTUPLE_DEAD and removes
        # it, so VACUUM finishes successfully and increments vacuum_count.
        session_b.sql("COMMIT")
        primary.poll_query_until(
            f"SELECT vacuum_count > 0 FROM pg_stat_all_tables WHERE relname = '{TABLE}'",
            dbname=TEST_DB,
        )
    finally:
        # Collect the in-flight VACUUM before teardown (re-raises on failure).
        vacuum.result()

    primary_lsn = primary.lsn("flush")
    # Make sure something causes us to flush.
    primary.sql_oneshot(f"INSERT INTO {TABLE} VALUES (1)", dbname=TEST_DB)

    # Nothing on the replica should cause a recovery conflict, so this finishes.
    primary.wait_for_catchup(replica, "replay", primary_lsn)

    session_a.close()
    session_b.close()
