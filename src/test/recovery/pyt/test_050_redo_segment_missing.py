# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/050_redo_segment_missing.pl.

Evaluates recovery when the WAL segment containing the redo record is missing
while the checkpoint record is in a different segment: startup must fail with a
FATAL about not finding the redo location referenced by the checkpoint record.
"""

import pathlib
import subprocess

from pypg import skip_unless_injection_points


def test_redo_segment_missing(create_pg):
    node = create_pg("testnode", conf=["log_checkpoints = on"])
    skip_unless_injection_points(node)
    conn = node.connect()

    conn.sql("CREATE EXTENSION injection_points")

    # Two wait-based injection points: 'create-checkpoint-initial' runs outside
    # the checkpoint's critical section (initializing the wait machinery's
    # shared memory), and 'create-checkpoint-run' has its callback run inside
    # the critical section after the redo record is logged.
    conn.sql("SELECT injection_points_attach('create-checkpoint-initial', 'wait')")
    conn.sql("SELECT injection_points_attach('create-checkpoint-run', 'wait')")

    # Run the checkpoint in the background; it pauses just after starting.
    checkpoint = node.background()
    checkpoint_done = checkpoint.asql("CHECKPOINT")

    node.wait_for_event("checkpointer", "create-checkpoint-initial")
    conn.sql("SELECT injection_points_wakeup('create-checkpoint-initial')")

    # Now in the middle of the checkpoint, after the redo record was logged.
    node.wait_for_event("checkpointer", "create-checkpoint-run")

    # Switch WAL so the redo record and the checkpoint record land in different
    # segments.
    conn.sql("SELECT pg_switch_wal()")

    log_offset = node.current_log_position()
    conn.sql("SELECT injection_points_wakeup('create-checkpoint-run')")
    checkpoint_done.result()
    node.wait_for_log("checkpoint complete", log_offset)
    checkpoint.quit()

    redo_lsn = conn.sql("SELECT redo_lsn FROM pg_control_checkpoint()")
    redo_walfile = conn.sql(f"SELECT pg_walfile_name('{redo_lsn}')")
    checkpoint_lsn = conn.sql("SELECT checkpoint_lsn FROM pg_control_checkpoint()")
    checkpoint_walfile = conn.sql(f"SELECT pg_walfile_name('{checkpoint_lsn}')")
    assert redo_walfile != checkpoint_walfile, (
        "redo and checkpoint records on different segments"
    )

    # Remove the WAL segment containing the redo record.
    (pathlib.Path(node.datadir) / "pg_wal" / redo_walfile).unlink()
    node.stop("immediate")

    # The server is expected to fail during recovery.
    try:
        node.pg_ctl("start")
    except subprocess.CalledProcessError:
        pass

    assert "could not find redo location" in node.log_since(0), (
        "ends with FATAL because it could not find redo location"
    )
