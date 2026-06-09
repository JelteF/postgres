# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/047_checkpoint_physical_slot.pl.

Verifies that when a physical slot is advanced while a checkpoint is in
progress, the slot's restart_lsn still points at a WAL segment that exists on
disk after an immediate restart.
"""

import pathlib

from pypg import skip_unless_injection_points

POINT = "checkpoint-before-old-wal-removal"
RESTART_LSN = (
    "SELECT restart_lsn FROM pg_replication_slots WHERE slot_name = 'slot_physical'"
)


def test_checkpoint_physical_slot(create_pg):
    node = create_pg("mike", conf=["wal_level = 'replica'"])
    skip_unless_injection_points(node)
    conn = node.connect()

    conn.sql("CREATE EXTENSION injection_points")

    conn.sql("SELECT pg_create_physical_replication_slot('slot_physical', true)")
    # Advance the slot to the current position so everything is "valid".
    conn.sql("SELECT pg_replication_slot_advance('slot_physical', pg_current_wal_lsn())")
    conn.sql("CHECKPOINT")  # flush state and set a baseline

    node.advance_wal(20)
    conn.sql("SELECT pg_replication_slot_advance('slot_physical', pg_current_wal_lsn())")
    conn.sql("CHECKPOINT")  # set a new restart LSN

    node.advance_wal(20)
    conn.sql(RESTART_LSN)

    # Run a checkpoint in the background and pause it right before old WAL
    # removal via the injection point.
    checkpoint = node.background()
    checkpoint.sql(f"SELECT injection_points_attach('{POINT}', 'wait')")
    checkpoint_done = checkpoint.asql("CHECKPOINT")
    node.wait_for_event("checkpointer", POINT)

    # Advance the slot (recomputing the required LSN), then let the checkpoint
    # continue and remove WAL the slot previously needed.
    conn.sql("SELECT pg_replication_slot_advance('slot_physical', pg_current_wal_lsn())")
    log_offset = node.current_log_position()
    conn.sql(f"SELECT injection_points_wakeup('{POINT}')")
    checkpoint_done.result()
    node.wait_for_log("checkpoint complete", log_offset)
    checkpoint.quit()

    # Abruptly restart, then confirm the WAL segment for the slot's restart_lsn
    # still exists.
    node.stop("immediate")
    node.start()
    conn = node.connect()

    restart_lsn = conn.sql(RESTART_LSN)
    restart_lsn_segment = conn.sql(f"SELECT pg_walfile_name('{restart_lsn}'::pg_lsn)")
    segpath = pathlib.Path(node.datadir) / "pg_wal" / restart_lsn_segment
    assert segpath.is_file(), (
        f"WAL segment {restart_lsn_segment} for physical slot's "
        f"restart_lsn {restart_lsn} exists"
    )
