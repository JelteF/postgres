# Copyright (c) 2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/052_checkpoint_segment_missing.pl.

Verifies crash-recovery behavior when the WAL segment holding the checkpoint
record referenced by pg_control is missing and there is no backup_label: the
startup process must fail with a FATAL about not locating a valid checkpoint
record.
"""

import pathlib
import subprocess


def test_checkpoint_segment_missing(create_pg):
    node = create_pg("testnode", conf={"log_checkpoints": True})
    # Force a checkpoint so pg_control points at a checkpoint record we target.
    node.sql("CHECKPOINT")
    checkpoint_walfile = node.sql(
        "SELECT pg_walfile_name(checkpoint_lsn) FROM pg_control_checkpoint()"
    )
    assert checkpoint_walfile, "derived checkpoint WAL file name"

    # Stop without a shutdown checkpoint, then remove the segment holding the
    # checkpoint record.
    node.stop("immediate")
    walpath = pathlib.Path(node.datadir) / "pg_wal" / checkpoint_walfile
    assert walpath.is_file(), f"checkpoint WAL file exists before deletion: {walpath}"
    walpath.unlink()
    assert not walpath.exists(), f"checkpoint WAL file removed: {walpath}"

    # The server is expected to fail during recovery, so start it without
    # waiting for readiness.
    try:
        node.pg_ctl("start")
    except subprocess.CalledProcessError:
        pass  # expected: recovery fails

    assert "could not locate a valid checkpoint record" in node.log_since(0), (
        "FATAL logged for missing checkpoint record (no backup_label path)"
    )
