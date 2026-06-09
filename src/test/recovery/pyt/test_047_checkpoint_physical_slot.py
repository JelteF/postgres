# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/047_checkpoint_physical_slot.pl.

Verifies that when a physical slot is advanced while a checkpoint is in
progress, the slot's restart_lsn still points at a WAL segment that exists on
disk after an immediate restart.
"""

import pathlib

import pypg

POINT = "checkpoint-before-old-wal-removal"
RESTART_LSN = (
    "SELECT restart_lsn FROM pg_replication_slots WHERE slot_name = 'slot_physical'"
)


@pypg.require_injection_points()
def test_checkpoint_physical_slot(create_pg):
    node = create_pg("mike", conf={"wal_level": "replica"})
    node.sql("CREATE EXTENSION injection_points")

    node.sql("SELECT pg_create_physical_replication_slot('slot_physical', true)")
    # Advance the slot to the current position so everything is "valid".
    node.sql(
        "SELECT pg_replication_slot_advance('slot_physical', pg_current_wal_lsn())"
    )
    node.sql("CHECKPOINT")  # flush state and set a baseline

    node.advance_wal(20)
    node.sql(
        "SELECT pg_replication_slot_advance('slot_physical', pg_current_wal_lsn())"
    )
    node.sql("CHECKPOINT")  # set a new restart LSN

    node.advance_wal(20)
    node.sql(RESTART_LSN)

    # Run a checkpoint in the background and pause it right before old WAL
    # removal via the injection point.
    node.sql(f"SELECT injection_points_attach('{POINT}', 'wait')")
    checkpoint_done = node.background_sql_oneshot("CHECKPOINT")
    node.wait_for_event("checkpointer", POINT)

    # Advance the slot (recomputing the required LSN), then let the checkpoint
    # continue and remove WAL the slot previously needed.
    node.sql(
        "SELECT pg_replication_slot_advance('slot_physical', pg_current_wal_lsn())"
    )
    log_offset = node.current_log_position()
    node.sql(f"SELECT injection_points_wakeup('{POINT}')")
    checkpoint_done.result()
    node.wait_for_log("checkpoint complete", log_offset)

    # Abruptly restart, then confirm the WAL segment for the slot's restart_lsn
    # still exists.
    node.stop("immediate")
    node.start()

    restart_lsn = node.sql(RESTART_LSN)
    restart_lsn_segment = node.sql(f"SELECT pg_walfile_name('{restart_lsn}'::pg_lsn)")
    segpath = pathlib.Path(node.datadir) / "pg_wal" / restart_lsn_segment
    assert segpath.is_file(), (
        f"WAL segment {restart_lsn_segment} for physical slot's "
        f"restart_lsn {restart_lsn} exists"
    )
