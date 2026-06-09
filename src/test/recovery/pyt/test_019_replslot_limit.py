# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/019_replslot_limit.pl.

Tests that max_slot_wal_keep_size limits the WAL kept by replication slots: a
physical slot's wal_status walks reserved -> extended -> unreserved -> lost as
WAL advances, an invalidated slot no longer holds back the WAL horizon, a slot
held by an active (frozen) walsender is terminated, and the inactive_since
column behaves for physical and logical slots.
"""

import os
import platform
import re
import signal


def test_replslot_limit(create_pg):
    # 1MB WAL segments make the size-based slot states easy to drive.
    primary = create_pg(
        "primary",
        allows_streaming=True,
        initdb_opts=["--wal-segsize=1"],
        conf={"min_wal_size": "2MB", "max_wal_size": "4MB", "log_checkpoints": True},
    )
    primary.sql("SELECT pg_create_physical_replication_slot('rep1')")

    # The slot state and remain are null before the first connection.
    assert primary.sql(
        "SELECT restart_lsn IS NULL, wal_status is NULL, safe_wal_size is NULL "
        "FROM pg_replication_slots WHERE slot_name = 'rep1'"
    ) == (True, True, True), 'check the state of non-reserved slot is "unknown"'

    backup = primary.backup("my_backup")
    standby = create_pg(
        "standby_1",
        from_backup=backup,
        streaming_primary=primary,
        start=False,
        conf={"primary_slot_name": "rep1"},
    )
    standby.start()

    # Wait until the primary has processed standby feedback and advanced the
    # slot's restart_lsn, which the following wal_status checks depend on.
    primary.wait_for_slot_catchup("rep1", "restart", primary.lsn("write"))
    standby.stop()

    def wal_status_null():
        return primary.sql(
            "SELECT wal_status, safe_wal_size IS NULL FROM pg_replication_slots "
            "WHERE slot_name = 'rep1'"
        )

    def wal_status():
        return primary.sql(
            "SELECT wal_status FROM pg_replication_slots WHERE slot_name = 'rep1'"
        )

    # The slot is "reserved" now.
    assert wal_status_null() == ("reserved", True), "check the catching-up state"

    # The slot is always safe while WAL fits in max_wal_size.
    primary.advance_wal(1)
    primary.sql("CHECKPOINT")
    assert wal_status_null() == ("reserved", True), (
        "check that it is safe if WAL fits in max_wal_size"
    )

    primary.advance_wal(4)
    primary.sql("CHECKPOINT")
    # Always safe while max_slot_wal_keep_size is not set.
    assert wal_status_null() == ("reserved", True), "check that slot is working"

    def reconnect_standby():
        standby.start()
        primary.wait_for_slot_catchup("rep1", "restart", primary.lsn("write"))
        standby.stop()

    reconnect_standby()

    # Set max_slot_wal_keep_size on the primary.
    primary.append_conf(max_slot_wal_keep_size="6MB")
    primary.pg_ctl("reload")
    assert wal_status() == "reserved", "check that max_slot_wal_keep_size is working"

    primary.advance_wal(2)
    primary.sql("CHECKPOINT")
    assert wal_status() == "reserved", (
        "check that slot remains reserved after advancing WAL"
    )

    reconnect_standby()

    # wal_keep_size overrides max_slot_wal_keep_size.
    primary.sql("ALTER SYSTEM SET wal_keep_size to '8MB'")
    primary.pg_ctl("reload")
    primary.advance_wal(6)
    assert wal_status() == "extended", (
        "check that wal_keep_size overrides max_slot_wal_keep_size"
    )
    primary.sql("ALTER SYSTEM SET wal_keep_size to 0")
    primary.pg_ctl("reload")

    reconnect_standby()

    # Advance WAL without a checkpoint; the slot moves to 'extended'.
    primary.advance_wal(6)
    assert wal_status() == "extended", 'check that the slot state changes to "extended"'

    # Do a checkpoint so the next checkpoint runs too early, then advance again;
    # remain goes to 0 and the slot becomes 'unreserved'.
    primary.sql("CHECKPOINT")
    primary.advance_wal(1)
    assert primary.sql(
        "SELECT wal_status, safe_wal_size <= 0 FROM pg_replication_slots "
        "WHERE slot_name = 'rep1'"
    ) == ("unreserved", True), 'check that the slot state changes to "unreserved"'

    # The standby can still connect before a checkpoint removes the WAL.
    reconnect_standby()
    assert not re.search(
        r"requested WAL segment [0-9A-F]+ has already been removed",
        standby.log_since(0),
    ), "check that required WAL segments are still available"

    # Create a checkpoint for stability, then prevent checkpoints while
    # advancing past the slot's WAL.
    primary.sql("CHECKPOINT")
    primary.sql("ALTER SYSTEM SET max_wal_size='40MB'")
    primary.pg_ctl("reload")

    logstart = primary.current_log_position()
    primary.advance_wal(7)

    # Another checkpoint should now invalidate the slot.
    primary.sql("ALTER SYSTEM RESET max_wal_size")
    primary.pg_ctl("reload")
    primary.sql("CHECKPOINT")
    primary.wait_for_log('invalidating obsolete replication slot "rep1"', logstart)

    assert primary.sql(
        "SELECT slot_name, active, restart_lsn IS NULL, wal_status, safe_wal_size "
        "FROM pg_replication_slots WHERE slot_name = 'rep1'"
    ) == ("rep1", False, True, "lost", None), (
        'check that the slot became inactive and the state "lost" persists'
    )

    primary.wait_for_log("checkpoint complete: ", logstart)

    # The invalidated slot shouldn't hold back the old-segment horizon (bug
    # #17103): a new reserving slot's restart segment should equal the oldest
    # remaining WAL file.
    redoseg = primary.sql(
        "SELECT pg_walfile_name(lsn) FROM pg_create_physical_replication_slot('s2', true)"
    )
    oldestseg = primary.sql(
        "SELECT pg_ls_dir AS f FROM pg_ls_dir('pg_wal') "
        r"WHERE pg_ls_dir ~ '^[0-9A-F]{24}$' ORDER BY 1 LIMIT 1"
    )
    primary.sql("SELECT pg_drop_replication_slot('s2')")
    assert oldestseg == redoseg, "check that segments have been removed"

    # The standby no longer can connect to the primary.
    logstart = standby.current_log_position()
    standby.start()
    standby.wait_for_log(
        'This replication slot has been invalidated due to "wal_removed".', logstart
    )
    primary.stop()
    standby.stop()

    # A slot with max_slot_wal_keep_size=0 must not block checkpoints.
    primary2 = create_pg(
        "primary2",
        allows_streaming=True,
        conf={"min_wal_size": "32MB", "max_wal_size": "32MB", "log_checkpoints": True},
    )
    primary2.sql("SELECT pg_create_physical_replication_slot('rep1')")
    backup2 = primary2.backup("my_backup2")
    primary2.stop()
    primary2.append_conf(max_slot_wal_keep_size=0)
    primary2.start()

    standby2 = create_pg(
        "standby_2",
        from_backup=backup2,
        streaming_primary=primary2,
        start=False,
        conf={"primary_slot_name": "rep1"},
    )
    standby2.start()
    primary2.advance_wal(1)
    assert primary2.sql_batch("CHECKPOINT", "SELECT 'finished'")[-1] == "finished", (
        "check if checkpoint command is not blocked"
    )
    primary2.stop()
    standby2.stop()

    # The remaining checks freeze the walsender with SIGSTOP, which isn't
    # available on Windows; the upstream TAP test likewise stops here on
    # Windows (its `kill` is not portable).
    if platform.system() == "Windows":
        return

    # Get a slot terminated while its walsender is active, by freezing the
    # walsender with SIGSTOP so the slot stays active but stops advancing.
    primary3 = create_pg(
        "primary3",
        allows_streaming=True,
        initdb_opts=["--wal-segsize=1"],
        conf={
            "min_wal_size": "2MB",
            "max_wal_size": "2MB",
            "log_checkpoints": True,
            "max_slot_wal_keep_size": "1MB",
        },
    )
    primary3.sql("SELECT pg_create_physical_replication_slot('rep3')")
    backup3 = primary3.backup("my_backup")
    standby3 = create_pg(
        "standby_3",
        from_backup=backup3,
        streaming_primary=primary3,
        start=False,
        conf={"primary_slot_name": "rep3"},
    )
    standby3.start()
    primary3.wait_for_catchup(standby3)

    primary3.poll_query_until(
        "SELECT count(*) = 1 FROM pg_stat_activity WHERE backend_type = 'walsender'"
    )
    senderpid = primary3.sql(
        "SELECT pid FROM pg_stat_activity WHERE backend_type = 'walsender'"
    )
    receiverpid = standby3.sql(
        "SELECT pid FROM pg_stat_activity WHERE backend_type = 'walreceiver'"
    )

    logstart = primary3.current_log_position()
    # Freeze walsender and walreceiver. The slot stays active but the
    # walreceiver no longer makes progress.
    os.kill(senderpid, signal.SIGSTOP)
    os.kill(receiverpid, signal.SIGSTOP)
    try:
        primary3.advance_wal(2)
        primary3.wait_for_log(
            f'terminating process {senderpid} to release replication slot "rep3"',
            logstart,
        )

        # Let the walsender continue; the slot should be killed now. The
        # walreceiver must stay frozen so the standby can't start a new one
        # before the slot is killed.
        os.kill(senderpid, signal.SIGCONT)
        primary3.poll_query_until(
            "SELECT wal_status FROM pg_replication_slots WHERE slot_name = 'rep3'",
            expected="lost",
        )
        primary3.wait_for_log('invalidating obsolete replication slot "rep3"', logstart)
    finally:
        os.kill(receiverpid, signal.SIGCONT)

    primary3.stop()
    standby3.stop()

    # Check inactive_since for a streaming standby's physical slot.
    primary4 = create_pg(
        "primary4", allows_streaming=True, conf={"wal_level": "logical"}
    )
    backup4 = primary4.backup("my_backup4")
    standby4 = create_pg(
        "standby4",
        from_backup=backup4,
        streaming_primary=primary4,
        start=False,
        conf={"primary_slot_name": "sb4_slot"},
    )

    def inactive_since_after(node, slot, reference):
        # The slot's inactive_since (set at creation/deactivation) must be later
        # than a timestamp captured beforehand; return it.
        return node.sql(
            f"SELECT inactive_since::text FROM pg_replication_slots "
            f"WHERE slot_name = '{slot}' AND inactive_since > '{reference}'::timestamptz"
        )

    reference = primary4.sql("SELECT current_timestamp::text")
    primary4.sql("SELECT pg_create_physical_replication_slot(slot_name := 'sb4_slot')")
    inactive_since = inactive_since_after(primary4, "sb4_slot", reference)

    standby4.start()
    primary4.wait_for_catchup(standby4)

    assert (
        primary4.sql(
            "SELECT inactive_since IS NULL FROM pg_replication_slots WHERE slot_name = 'sb4_slot'"
        )
        is True
    ), "last inactive time for an active physical slot is NULL"

    standby4.stop()
    # Restart so inactive_since is set when loading the slot from disk.
    primary4.pg_ctl("restart")
    assert (
        primary4.sql(
            f"SELECT inactive_since > '{inactive_since}'::timestamptz "
            "FROM pg_replication_slots WHERE slot_name = 'sb4_slot' AND inactive_since IS NOT NULL"
        )
        is True
    ), "last inactive time for an inactive physical slot is updated correctly"

    # Check inactive_since for a logical subscriber's slot.
    publisher4 = primary4
    subscriber4 = create_pg("subscriber4")
    publisher4_connstr = publisher4.connstr() + " dbname=postgres"
    publisher4.sql("CREATE PUBLICATION pub FOR ALL TABLES")

    reference = publisher4.sql("SELECT current_timestamp::text")
    publisher4.sql(
        "SELECT pg_create_logical_replication_slot(slot_name := 'lsub4_slot', "
        "plugin := 'pgoutput')"
    )
    inactive_since = inactive_since_after(publisher4, "lsub4_slot", reference)

    subscriber4.sql(
        f"CREATE SUBSCRIPTION sub CONNECTION '{publisher4_connstr}' PUBLICATION pub "
        "WITH (slot_name = 'lsub4_slot', create_slot = false)"
    )
    subscriber4.wait_for_subscription_sync(publisher4, "sub")

    assert (
        publisher4.sql(
            "SELECT inactive_since IS NULL FROM pg_replication_slots WHERE slot_name = 'lsub4_slot'"
        )
        is True
    ), "last inactive time for an active logical slot is NULL"

    subscriber4.stop()
    publisher4.pg_ctl("restart")
    assert (
        publisher4.sql(
            f"SELECT inactive_since > '{inactive_since}'::timestamptz "
            "FROM pg_replication_slots WHERE slot_name = 'lsub4_slot' AND inactive_since IS NOT NULL"
        )
        is True
    ), "last inactive time for an inactive logical slot is updated correctly"
