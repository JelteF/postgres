# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/051_effective_wal_level.pl.

Tests that effective_wal_level tracks logical replication slot creation and
deletion independently of the configured wal_level: it rises to 'logical' when
the first logical slot is created (even at wal_level='replica'), persists
across restarts and on standbys/cascades following the primary's value, falls
back to 'replica' when the last valid logical slot is gone or invalidated, and
that the activation can be safely aborted, also while a concurrent activation
is in progress.
"""

import subprocess

import pytest

from libpq import LibpqError


def _injection_points_available(node):
    return node.sql(
        "SELECT count(*) > 0 FROM pg_available_extensions WHERE name = 'injection_points'"
    )


def test_effective_wal_level(create_pg):
    def wal_level(node):
        return node.sql(
            "SELECT current_setting('wal_level'), current_setting('effective_wal_level')"
        )

    def wait_logical_disabled(node):
        node.poll_query_until(
            "SELECT current_setting('effective_wal_level') = 'replica'"
        )

    primary = create_pg(
        "primary", allows_streaming=True, conf={"log_min_messages": "debug1"}
    )

    assert wal_level(primary) == ("replica", "replica"), (
        "wal_level and effective_wal_level start as 'replica'"
    )

    # A physical slot doesn't affect effective_wal_level.
    primary.sql(
        "SELECT pg_create_physical_replication_slot('test_phy_slot', false, false)"
    )
    assert wal_level(primary) == ("replica", "replica"), (
        "effective_wal_level doesn't change with a new physical slot"
    )
    primary.sql("SELECT pg_drop_replication_slot('test_phy_slot')")

    # A temporary logical slot enables logical decoding then, on session end,
    # delegates disabling it to the checkpointer.
    enable_msg = (
        "logical decoding is enabled upon creating a new logical replication slot"
    )
    offset = primary.current_log_position()
    # A temp slot is dropped when its session ends, which is what hands off
    # disabling logical decoding to the checkpointer; use a one-shot
    # connection so that happens right after this statement instead of only
    # when the cached default connection eventually closes.
    primary.sql_oneshot(
        "SELECT pg_create_logical_replication_slot('test_tmp_slot', 'test_decoding', true)"
    )
    assert enable_msg in primary.log_since(offset), (
        "logical decoding enabled upon creating a temp slot"
    )
    wait_logical_disabled(primary)

    # Logical decoding is also disabled again after a REPACK that used it.
    primary.sql("CREATE TABLE foo(a int primary key)")
    offset = primary.current_log_position()
    primary.sql("REPACK (concurrently) foo")
    assert enable_msg in primary.log_since(offset), "logical decoding enabled by repack"
    wait_logical_disabled(primary)
    assert wal_level(primary) == ("replica", "replica"), (
        "logical decoding disabled after repack"
    )

    # A persistent logical slot raises effective_wal_level to 'logical'.
    primary.sql("SELECT pg_create_logical_replication_slot('test_slot', 'pgoutput')")
    assert wal_level(primary) == ("replica", "logical"), (
        "effective_wal_level increased to 'logical' upon a logical slot creation"
    )

    # It persists across a restart.
    primary.pg_ctl("restart")
    assert wal_level(primary) == ("replica", "logical"), (
        "effective_wal_level remains 'logical' after a server restart"
    )

    # Creating and dropping another slot leaves it 'logical' (one slot remains).
    primary.sql("SELECT pg_create_logical_replication_slot('test_slot2', 'pgoutput')")
    primary.sql("SELECT pg_drop_replication_slot('test_slot2')")
    assert wal_level(primary) == ("replica", "logical"), (
        "effective_wal_level stays 'logical' as one slot remains"
    )

    # The server can't start with wal_level='minimal' while a slot exists.
    primary.adjust_conf(wal_level="minimal")
    primary.adjust_conf(max_wal_senders="0")
    primary.stop()
    offset = primary.current_log_position()
    with pytest.raises(subprocess.CalledProcessError):
        primary.start()
    assert (
        'logical replication slot "test_slot" exists, but "wal_level" < "replica"'
        in primary.log_since(offset)
    ), "logical slots require logical decoding enabled at server startup"

    # Revert, and add settings to test disabling logical decoding when the last
    # logical slot is invalidated.
    primary.adjust_conf(wal_level="replica")
    primary.adjust_conf(max_wal_senders="10")
    primary.append_conf(
        min_wal_size="32MB", max_wal_size="32MB", max_slot_wal_keep_size="16MB"
    )
    primary.start()

    # Advance WAL so the slot gets invalidated.
    primary.advance_wal(2)
    primary.sql("CHECKPOINT")
    assert (
        primary.sql(
            "SELECT invalidation_reason = 'wal_removed' FROM pg_replication_slots "
            "WHERE slot_name = 'test_slot'"
        )
        is True
    ), "test_slot gets invalidated due to wal_removed"

    wait_logical_disabled(primary)
    assert wal_level(primary) == ("replica", "replica"), (
        "effective_wal_level decreased to 'replica' after invalidating the last slot"
    )

    # Revert the WAL-size settings and restart.
    primary.adjust_conf(max_slot_wal_keep_size=None)
    primary.adjust_conf(min_wal_size=None)
    primary.adjust_conf(max_wal_size=None)
    primary.pg_ctl("restart")

    # Recreate the logical slot to enable logical decoding again.
    primary.sql("SELECT pg_drop_replication_slot('test_slot')")
    primary.sql("SELECT pg_create_logical_replication_slot('test_slot', 'pgoutput')")

    # Take a backup while effective_wal_level is 'logical' (slots are excluded).
    backup = primary.backup("my_backup")

    standby1 = create_pg("standby1", from_backup=backup, streaming_primary=primary)

    # Creating a logical slot on the standby succeeds as the primary enables it.
    primary.wait_for_catchup(standby1)
    standby1.create_logical_slot_on_standby(primary, "standby1_slot", "postgres")

    # Promoting standby1 (which has a logical slot) keeps it 'logical'.
    standby1.promote()
    assert wal_level(standby1) == ("replica", "logical"), (
        "effective_wal_level remains 'logical' after the promotion"
    )
    # And a logical slot can be created after the promotion.
    standby1.sql(
        "SELECT pg_create_logical_replication_slot('standby1_slot2', 'pgoutput')"
    )
    standby1.stop()

    # standby2 starts with wal_level='logical'.
    standby2 = create_pg(
        "standby2",
        from_backup=backup,
        streaming_primary=primary,
        start=False,
        conf={"wal_level": "logical"},
    )
    standby2.start()
    backup3 = standby2.backup("my_backup3")

    # A cascaded standby starting with wal_level='replica'.
    cascade = create_pg(
        "cascade",
        from_backup=backup3,
        streaming_primary=standby2,
        start=False,
    )
    cascade.adjust_conf(wal_level="replica")
    cascade.start()

    # effective_wal_level on the standby and cascade follow the primary.
    assert wal_level(standby2) == ("logical", "logical"), "wal levels on standby"
    assert wal_level(cascade) == ("replica", "logical"), (
        "wal levels on cascaded standby"
    )

    # Dropping the primary's last logical slot decreases effective_wal_level
    # everywhere.
    primary.sql("SELECT pg_drop_replication_slot('test_slot')")
    wait_logical_disabled(primary)
    lsn = primary.lsn("flush")
    primary.wait_for_catchup(standby2, "replay", lsn)
    standby2.wait_for_catchup(cascade, "replay", lsn)

    assert wal_level(primary) == ("replica", "replica"), (
        "decreased to 'replica' on primary"
    )
    assert wal_level(standby2) == ("logical", "replica"), (
        "decreased to 'replica' on standby"
    )
    assert wal_level(cascade) == ("replica", "replica"), (
        "decreased to 'replica' on cascade"
    )

    # Promoting standby2 (wal_level=logical) raises effective_wal_level on the
    # cascade, which now follows it.
    standby2.promote()
    standby2.wait_for_catchup(cascade)
    assert wal_level(cascade) == ("replica", "logical"), (
        "increased to 'logical' on cascade as the new primary has wal_level='logical'"
    )
    standby2.stop()
    cascade.stop()

    # standby3 streams from the primary.
    standby3 = create_pg("standby3", from_backup=backup, streaming_primary=primary)

    primary.sql("SELECT pg_create_logical_replication_slot('test_slot', 'pgoutput')")
    primary.wait_for_catchup(standby3)
    standby3.create_logical_slot_on_standby(primary, "standby3_slot", "postgres")

    # Dropping the primary's slot decreases effective_wal_level there, which
    # invalidates the standby's slot for 'wal_level_insufficient'.
    primary.sql("SELECT pg_drop_replication_slot('test_slot')")
    wait_logical_disabled(primary)
    assert wal_level(primary) == ("replica", "replica"), (
        "decreased to 'replica' on the primary to invalidate standby's slots"
    )
    standby3.poll_query_until(
        "SELECT invalidation_reason = 'wal_level_insufficient' FROM pg_replication_slots "
        "WHERE slot_name = 'standby3_slot'"
    )

    # The invalidated slot is restored across a restart.
    standby3.pg_ctl("restart")
    assert wal_level(standby3) == ("replica", "replica"), (
        "decreased to 'replica' on standby"
    )

    with pytest.raises(
        LibpqError,
        match='logical decoding on standby requires "effective_wal_level" >= "logical" '
        "on the primary",
    ):
        standby3.sql("SELECT pg_logical_slot_get_changes('standby3_slot', null, null)")

    # Restart the primary with wal_level='logical' and a new slot.
    primary.append_conf(wal_level="logical")
    primary.pg_ctl("restart")
    primary.sql("SELECT pg_create_logical_replication_slot('test_slot', 'pgoutput')")
    primary.wait_for_catchup(standby3)
    assert wal_level(primary) == ("logical", "logical"), "WAL levels on the primary"
    assert wal_level(standby3) == ("replica", "logical"), (
        "increased to 'logical' again on standby"
    )

    # Setting wal_level back to 'replica' keeps effective_wal_level 'logical'
    # since a logical slot remains.
    primary.adjust_conf(wal_level="replica")
    primary.pg_ctl("restart")
    primary.wait_for_catchup(standby3)
    assert wal_level(primary) == ("replica", "logical"), (
        "remains 'logical' on primary even after setting wal_level to 'replica'"
    )
    assert wal_level(standby3) == ("replica", "logical"), (
        "remains 'logical' on standby even after wal_level set to 'replica' on primary"
    )

    # Promoting standby3 with no valid logical slot drops it to 'replica'.
    standby3.promote()
    assert wal_level(standby3) == ("replica", "replica"), (
        "decreased to 'replica' as there is no valid logical slot"
    )
    standby3.sql("SELECT pg_drop_replication_slot('standby3_slot')")
    standby3.stop()

    # The remaining tests exercise injection points; skip if unavailable.
    if not _injection_points_available(primary):
        return

    # Race between startup and the logical-decoding status change at end of
    # recovery.
    standby4 = create_pg("standby4", from_backup=backup, streaming_primary=primary)
    primary.wait_for_catchup(standby4)
    standby4.create_logical_slot_on_standby(primary, "standby4_slot", "postgres")

    primary.sql("CREATE EXTENSION injection_points")
    primary.wait_for_catchup(standby4)
    standby4.sql(
        "SELECT injection_points_attach("
        "'startup-logical-decoding-status-change-end-of-recovery', 'wait')"
    )

    # Promote without waiting, and wait for the startup process to reach the
    # injection point.
    standby4.sql("SELECT pg_promote(false)")
    standby4.wait_for_event(
        "startup", "startup-logical-decoding-status-change-end-of-recovery"
    )

    # Drop the logical slot, requesting the checkpointer disable logical decoding.
    standby4.sql("SELECT pg_drop_replication_slot('standby4_slot')")
    standby4.sql(
        "SELECT injection_points_wakeup("
        "'startup-logical-decoding-status-change-end-of-recovery')"
    )

    wait_logical_disabled(standby4)
    assert wal_level(standby4) == ("replica", "replica"), (
        "effective_wal_level properly decreased to 'replica'"
    )
    standby4.stop()

    # Test aborting the logical-decoding activation process. Drop the primary's
    # slot first to decrease its effective_wal_level to 'replica'.
    primary.sql("SELECT pg_drop_replication_slot('test_slot')")
    wait_logical_disabled(primary)
    assert wal_level(primary) == ("replica", "replica"), (
        "decreased to 'replica' on primary"
    )

    # Start creating a logical slot, which activates logical decoding but waits
    # at the injection point.
    creating = primary.connect()
    creating.sql("SELECT injection_points_set_local()")
    creating.sql(
        "SELECT injection_points_attach('logical-decoding-activation', 'wait')"
    )
    create_future = creating.background_sql(
        "SELECT pg_create_logical_replication_slot('slot_canceled', 'pgoutput')"
    )
    try:
        primary.wait_for_event("client backend", "logical-decoding-activation")
        # Cancel that backend, aborting the activation.
        offset = primary.current_log_position()
        primary.sql(
            "SELECT pg_cancel_backend(pid) FROM pg_stat_activity "
            "WHERE query ~ 'slot_canceled' AND pid <> pg_backend_pid()"
        )
        primary.wait_for_log("aborting logical decoding activation process", offset)
        # The abort delegates undoing the partial activation to the
        # checkpointer, so wait for it rather than checking immediately.
        wait_logical_disabled(primary)
    finally:
        with pytest.raises(LibpqError):
            create_future.result()
        creating.close()

    # The canceled session's local injection point is only detached once its
    # backend has processed the disconnect, which happens asynchronously. Wait
    # for the detach before re-attaching the same point below.
    primary.poll_query_until(
        "SELECT count(*) = 0 FROM injection_points_list() "
        "WHERE point_name = 'logical-decoding-activation'"
    )

    # Test concurrent activation processes where one is interrupted. One
    # session again stops in the middle of the activation process.
    creating = primary.connect()
    creating.sql("SELECT injection_points_set_local()")
    creating.sql(
        "SELECT injection_points_attach('logical-decoding-activation', 'wait')"
    )
    create_future = creating.background_sql(
        "SELECT pg_create_logical_replication_slot('slot_canceled2', 'pgoutput')"
    )
    try:
        primary.wait_for_event("client backend", "logical-decoding-activation")

        # Another backend concurrently enables logical decoding; the stalled
        # activation must not block it.
        primary.sql(
            "SELECT pg_create_logical_replication_slot('test_slot2', 'pgoutput')"
        )
        assert wal_level(primary) == ("replica", "logical"), (
            "the concurrent activation has done properly"
        )

        # Cancel the stalled backend, aborting its activation process. This
        # must not affect the concurrently created slot.
        offset = primary.current_log_position()
        primary.sql(
            "SELECT pg_cancel_backend(pid) FROM pg_stat_activity "
            "WHERE query ~ 'slot_canceled2' AND pid <> pg_backend_pid()"
        )
        primary.wait_for_log("canceling statement due to user request", offset)
        assert wal_level(primary) == ("replica", "logical"), (
            "effective_wal_level remains 'logical' even after the concurrent "
            "activation is interrupted"
        )
    finally:
        with pytest.raises(LibpqError, match="canceling statement due to user request"):
            create_future.result()
        creating.close()
