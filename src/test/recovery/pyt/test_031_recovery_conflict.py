# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/031_recovery_conflict.pl.

Test that connections to a hot standby are correctly canceled when a recovery
conflict is detected, and that pg_stat_database_conflicts is populated. Each
conflict type kills the standby session that triggers it, so a fresh background
session is opened per scenario (the Perl test reconnects one psql).
"""

from libpq import LibpqError
import pytest

from pypg._env import test_timeout_default

TABLESPACE1 = "test_recovery_conflict_tblspc"
TEST_DB = "test_db"
TABLE1 = "test_recovery_conflict_table1"
TABLE2 = "test_recovery_conflict_table2"
CURSOR1 = "test_recovery_conflict_cursor"


def test_recovery_conflict(create_pg):
    primary = create_pg(
        "primary",
        allows_streaming=True,
        conf={
            "allow_in_place_tablespaces": True,
            "log_temp_files": 0,
            # for deadlock test
            "max_prepared_transactions": 10,
            # wait some to test the wait paths as well, but not long
            "max_standby_streaming_delay": "50ms",
            "temp_tablespaces": TABLESPACE1,
            # Some recovery-conflict logging is only exercised after
            # deadlock_timeout; give minimal coverage of that code.
            "log_recovery_conflict_waits": True,
            "deadlock_timeout": "10ms",
        },
    )
    primary.sql(f"CREATE TABLESPACE {TABLESPACE1} LOCATION ''")

    backup = primary.backup("my_backup")
    standby = create_pg("standby", from_backup=backup, streaming_primary=primary)

    # Use a new database to be able to trigger a database recovery conflict.
    primary.sql(f"CREATE DATABASE {TEST_DB}")
    primary.sql_batch_oneshot(
        f"CREATE TABLE {TABLE1}(a int, b int)",
        f"INSERT INTO {TABLE1} SELECT i % 3, 0 FROM generate_series(1,20) i",
        f"CREATE TABLE {TABLE2}(a int, b int)",
        dbname=TEST_DB,
    )
    primary.wait_for_catchup(standby)

    def check_conflict_stat(conflict_type):
        # Poll rather than read once: the startup process flushes recovery
        # conflict stats to shared memory with a small delay.
        standby.poll_query_until(
            f"SELECT confl_{conflict_type} = 1 FROM pg_stat_database_conflicts "
            f"WHERE datname='{TEST_DB}'",
            dbname=TEST_DB,
        )

    expected_conflicts = 0

    # RECOVERY CONFLICT 1: Buffer pin conflict.
    expected_conflicts += 1
    # Aborted INSERT to be cleaned up by vacuum, old enough that there's no
    # snapshot conflict before the buffer pin conflict. The statements run on one
    # held connection so the explicit transactions behave as in psql (a single
    # multi-statement PQexec would merge them into one implicit transaction).
    with primary.connect(dbname=TEST_DB) as c:
        c.sql("BEGIN")
        c.sql(f"INSERT INTO {TABLE1} VALUES (1,0)")
        c.sql("ROLLBACK")
        # ensure flush, rollback doesn't do so
        c.sql("BEGIN")
        c.sql(f"LOCK {TABLE1}")
        c.sql("COMMIT")
    primary.wait_for_catchup(standby)

    # A cursor on the standby pins the only block of the relation.
    bg = standby.connect(dbname=TEST_DB)
    res = bg.sql_batch(
        f"BEGIN",
        f"DECLARE {CURSOR1} CURSOR FOR SELECT b FROM {TABLE1}",
        f"FETCH FORWARD FROM {CURSOR1}",
    )[-1]
    assert res == 0, "buffer pin conflict: cursor with conflicting pin established"

    offset = standby.current_log_position()
    primary.sql_oneshot(f"VACUUM FREEZE {TABLE1}", dbname=TEST_DB)
    # Existing connection is terminated before replay finishes, so waiting for
    # catchup avoids a race between the conflict disconnect and the log check.
    primary.wait_for_catchup(standby)
    standby.wait_for_log("User was holding shared buffer pin for too long", offset)
    bg.close()
    check_conflict_stat("bufferpin")

    # RECOVERY CONFLICT 2: Snapshot conflict.
    expected_conflicts += 1
    primary.sql_oneshot(
        f"INSERT INTO {TABLE1} SELECT i, 0 FROM generate_series(1,20) i", dbname=TEST_DB
    )
    primary.wait_for_catchup(standby)

    bg = standby.connect(dbname=TEST_DB)
    res = bg.sql_batch(
        f"BEGIN",
        f"DECLARE {CURSOR1} CURSOR FOR SELECT b FROM {TABLE1}",
        f"FETCH FORWARD FROM {CURSOR1}",
    )[-1]
    assert res == 0, "snapshot conflict: cursor with conflicting snapshot established"

    # HOT updates, then VACUUM FREEZE pruning those dead tuples.
    primary.sql_oneshot(f"UPDATE {TABLE1} SET a = a + 1 WHERE a > 2", dbname=TEST_DB)
    offset = standby.current_log_position()
    primary.sql_oneshot(f"VACUUM FREEZE {TABLE1}", dbname=TEST_DB)
    primary.wait_for_catchup(standby)
    standby.wait_for_log(
        "User query might have needed to see row versions that must be removed", offset
    )
    bg.close()
    check_conflict_stat("snapshot")

    # RECOVERY CONFLICT 3: Lock conflict.
    expected_conflicts += 1
    bg = standby.connect(dbname=TEST_DB)
    res = bg.sql_batch(
        f"BEGIN", f"LOCK TABLE {TABLE1} IN ACCESS SHARE MODE", "SELECT 1"
    )[-1]
    assert res == 1, "lock conflict: conflicting lock acquired"

    offset = standby.current_log_position()
    primary.sql_oneshot(f"DROP TABLE {TABLE1}", dbname=TEST_DB)
    primary.wait_for_catchup(standby)
    standby.wait_for_log("User was holding a relation lock for too long", offset)
    bg.close()
    check_conflict_stat("lock")

    # RECOVERY CONFLICT 4: Tablespace conflict.
    expected_conflicts += 1
    # A cursor whose query spills tuples into temp files in the temp tablespace.
    bg = standby.connect(dbname=TEST_DB)
    res = bg.sql_batch(
        f"BEGIN",
        f"SET work_mem = '64kB'",
        f"DECLARE {CURSOR1} CURSOR FOR SELECT count(*) FROM generate_series(1,6000)",
        f"FETCH FORWARD FROM {CURSOR1}",
    )[-1]
    assert res == 6000, (
        "tablespace conflict: cursor with conflicting temp file established"
    )

    offset = standby.current_log_position()
    primary.sql_oneshot(f"DROP TABLESPACE {TABLESPACE1}", dbname=TEST_DB)
    primary.wait_for_catchup(standby)
    standby.wait_for_log(
        "User was or might have been using tablespace that must be dropped", offset
    )
    bg.close()
    check_conflict_stat("tablespace")

    # RECOVERY CONFLICT 5: Deadlock.
    expected_conflicts += 1
    # Test recovery deadlock conflicts, not buffer pin conflicts: without a
    # larger max_standby_streaming_delay it'd be timing-dependent which we hit.
    standby.append_conf(max_standby_streaming_delay=f"{test_timeout_default()}s")
    standby.pg_ctl("restart")

    # Generate dead rows for vacuum to clean up later. Then hold a lock on
    # another relation in a prepared xact, continuously held by the startup
    # process. The standby session blocks acquiring that lock while holding a
    # pin vacuum needs, triggering the deadlock.
    with primary.connect(dbname=TEST_DB) as setup:
        setup.sql(f"CREATE TABLE {TABLE1}(a int, b int)")
        setup.sql(f"INSERT INTO {TABLE1} VALUES (1)")
        with primary.connect(dbname=TEST_DB) as c:
            c.sql("BEGIN")
            c.sql(f"INSERT INTO {TABLE1}(a) SELECT generate_series(1, 100) i")
            c.sql("ROLLBACK")
            # The prepared transaction holds the lock on TABLE2 continuously,
            # independently of this session.
            c.sql_batch(f"BEGIN", f"LOCK TABLE {TABLE2}", "PREPARE TRANSACTION 'lock'")
        setup.sql(f"INSERT INTO {TABLE1}(a) VALUES (170)")
        setup.sql("SELECT txid_current()")
    primary.wait_for_catchup(standby)

    bg = standby.connect(dbname=TEST_DB)
    bg.sql("BEGIN")
    # hold pin
    bg.sql(f"DECLARE {CURSOR1} CURSOR FOR SELECT a FROM {TABLE1}")
    assert bg.sql(f"FETCH FORWARD FROM {CURSOR1}") == 1
    # wait for lock held by the prepared transaction (blocks)
    waiter = bg.background_sql(f"SELECT * FROM {TABLE2}")

    try:
        # Make sure we're already waiting for the lock.
        standby.poll_query_until(
            "SELECT 'waiting' FROM pg_locks WHERE locktype = 'relation' AND NOT granted",
            expected="waiting",
        )

        # VACUUM FREEZE prunes rows, causing a buffer pin conflict while the
        # standby session waits on the lock -> recovery deadlock.
        offset = standby.current_log_position()
        primary.sql_oneshot(f"VACUUM FREEZE {TABLE1}", dbname=TEST_DB)
        primary.wait_for_catchup(standby)
        standby.wait_for_log(
            "User transaction caused buffer deadlock with recovery.", offset
        )
    finally:
        # Unlike the other conflicts this one resolves by canceling the
        # statement (ERROR), not terminating the connection, so the session
        # survives with an aborted transaction.
        with pytest.raises(LibpqError):
            waiter.result()
    # Disconnect so the backend exits and flushes its pending conflict stat to
    # shared memory (an idle surviving backend would not flush it in time).
    bg.close()
    check_conflict_stat("deadlock")

    # Clean up for the next tests.
    primary.sql_oneshot("ROLLBACK PREPARED 'lock'", dbname=TEST_DB)
    standby.append_conf(max_standby_streaming_delay="50ms")
    standby.pg_ctl("restart")

    # Check the conflict count in pg_stat_database before the database is dropped.
    assert (
        standby.sql_oneshot(
            f"SELECT conflicts FROM pg_stat_database WHERE datname='{TEST_DB}'",
            dbname=TEST_DB,
        )
        == expected_conflicts
    ), f"{expected_conflicts} recovery conflicts shown in pg_stat_database"

    # RECOVERY CONFLICT 6: Database conflict. A live standby connection to the
    # database is terminated when the drop is replayed.
    db_conn = standby.connect(dbname=TEST_DB)
    db_conn.sql("SELECT 1")
    offset = standby.current_log_position()
    primary.sql(f"DROP DATABASE {TEST_DB}")
    primary.wait_for_catchup(standby)
    standby.wait_for_log(
        "User was connected to a database that must be dropped", offset
    )
    db_conn.close()
