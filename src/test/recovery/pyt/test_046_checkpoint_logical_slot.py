# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/046_checkpoint_logical_slot.pl.

Verifies the case where a logical slot is advanced during a checkpoint: the
slot's restart_lsn must still refer to an existing WAL segment after an
immediate restart. Also verifies that synchronized slots are not invalidated
immediately after synchronization in the presence of a concurrent checkpoint.
"""

import threading

import pypg


@pypg.require_injection_points()
def test_checkpoint_logical_slot(create_pg):
    node = create_pg("mike", allows_streaming=True, conf={"wal_level": "logical"})
    node.sql("CREATE EXTENSION injection_points")

    # Create the two slots we need.
    node.sql(
        "SELECT pg_create_logical_replication_slot('slot_logical', 'test_decoding')"
    )
    node.sql("SELECT pg_create_physical_replication_slot('slot_physical', true)")

    # Advance both slots to the current position to make everything "valid".
    node.sql(
        "SELECT count(*) FROM pg_logical_slot_get_changes('slot_logical', null, null)"
    )
    node.sql(
        "SELECT pg_replication_slot_advance('slot_physical', pg_current_wal_lsn())"
    )

    # Flush current state to disk and set a baseline.
    node.sql("CHECKPOINT")

    # Keep generating transactions to get RUNNING_XACTS while we work.
    with node.repeat_query("SELECT 1", interval=0.1):
        node.advance_wal(20)
        node.sql("CHECKPOINT")
        node.advance_wal(20)

        # Run a checkpoint in the background and make it wait on the injection
        # point so it stops right before removing old WAL segments.
        node.sql(
            "SELECT injection_points_attach('checkpoint-before-old-wal-removal', 'wait')"
        )
        checkpoint_future = node.background_sql_oneshot("CHECKPOINT")
        node.wait_for_event("checkpointer", "checkpoint-before-old-wal-removal")

        # Advance the logical slot, making it stop when it moves to the next WAL
        # segment. The injection fires only on a confirm that crosses a segment
        # boundary, so (like the Perl test's \watch) keep calling get_changes on
        # a background connection until one call parks at the injection point.
        node.sql(
            "SELECT injection_points_attach('logical-replication-slot-advance-segment', 'wait')"
        )
        logical_conn = node.connect()
        stop_logical = threading.Event()

        def logical_loop():
            while not stop_logical.is_set():
                try:
                    logical_conn.sql(
                        "SELECT count(*) FROM "
                        "pg_logical_slot_get_changes('slot_logical', null, null)"
                    )
                except Exception:
                    return
                stop_logical.wait(1)

        logical_thread = threading.Thread(target=logical_loop, daemon=True)
        logical_thread.start()
        node.wait_for_event(
            "client backend", "logical-replication-slot-advance-segment"
        )

        # Advance the physical slot, which recalculates the required LSN, then
        # unblock the checkpoint, which removes WAL still needed by the logical
        # slot.
        node.sql(
            "SELECT pg_replication_slot_advance('slot_physical', pg_current_wal_lsn())"
        )

        # Generate a long WAL record, spanning at least two pages, for the
        # post-recovery check.
        node.sql("SELECT pg_logical_emit_message(false, '', repeat('123456789', 1000))")

        # Continue the checkpoint and wait for its completion.
        log_offset = node.current_log_position()
        node.sql("SELECT injection_points_wakeup('checkpoint-before-old-wal-removal')")
        node.wait_for_log("checkpoint complete", log_offset)

    # Abruptly stop the server. Like the Perl test, the background checkpoint
    # and logical-advance sessions are abandoned rather than waited on (the
    # injection point stays attached, so checkpoints keep re-parking); the
    # immediate stop releases both so their workers exit.
    node.stop("immediate")
    stop_logical.set()
    logical_thread.join(timeout=30)
    logical_conn.close()
    try:
        checkpoint_future.result()
    except Exception:
        pass
    node.start()

    # The logical slot must still be valid: its restart_lsn refers to an
    # existing WAL segment.
    node.sql(
        "SELECT count(*) FROM pg_logical_slot_get_changes('slot_logical', null, null)"
    )

    # Verify that synchronized slots are not invalidated immediately after
    # synchronization in the presence of a concurrent checkpoint.
    primary = node
    primary.append_conf(autovacuum=False)
    primary.pg_ctl("reload")
    backup = primary.backup("backup")

    standby = create_pg(
        "standby",
        from_backup=backup,
        streaming_primary=primary,
        start=False,
        conf={"hot_standby_feedback": True, "primary_slot_name": "phys_slot"},
    )

    # A failover logical slot to be synced, and the physical slot the standby
    # streams through.
    primary.sql(
        "SELECT pg_create_logical_replication_slot('failover_slot', 'test_decoding', "
        "false, false, true)"
    )
    primary.sql("SELECT pg_create_physical_replication_slot('phys_slot')")

    standby.start()

    # Generate activity and switch WAL on the primary.
    primary.advance_wal(1)
    primary.sql("CHECKPOINT")
    primary.wait_for_catchup(standby)

    # Run a checkpoint (restartpoint) on the standby and make it wait right
    # before invalidating replication slots.
    standby.sql(
        "SELECT injection_points_attach('restartpoint-before-slot-invalidation', 'wait')"
    )
    restartpoint_future = standby.background_sql_oneshot("CHECKPOINT")
    standby.wait_for_event("checkpointer", "restartpoint-before-slot-invalidation")

    # Enable the slot sync worker to synchronize the failover slot.
    standby.append_conf(sync_replication_slots=True)
    standby.pg_ctl("reload")
    standby.poll_query_until(
        "SELECT COUNT(*) > 0 FROM pg_replication_slots WHERE slot_name = 'failover_slot'"
    )

    # Release the checkpointer.
    standby.sql(
        "SELECT injection_points_wakeup('restartpoint-before-slot-invalidation')"
    )
    standby.sql(
        "SELECT injection_points_detach('restartpoint-before-slot-invalidation')"
    )
    restartpoint_future.result()

    # The synchronized slot must not be invalidated.
    assert (
        standby.sql(
            "SELECT invalidation_reason IS NULL AND synced FROM pg_replication_slots "
            "WHERE slot_name = 'failover_slot'"
        )
        is True
    ), "logical slot is not invalidated"
