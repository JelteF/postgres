# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/042_low_level_backup.pl.

Tests the low-level backup method using pg_backup_start()/pg_backup_stop() with
a manual filesystem copy. Recovering the copy without backup_label must fall
back to crash recovery from the (advanced) control file and miss data created
after the backup started; recovering with backup_label restored into place uses
the correct redo location and sees that data.
"""

import pathlib
import re
import shutil
import subprocess

import pytest

CANARY_QUERY = "select count(*) from pg_class where relname = 'canary'"


def test_low_level_backup(create_pg, tmp_path):
    primary = create_pg("primary", archiving=True, allows_streaming=True)

    # Hold the non-exclusive backup open on one session while copying files.
    psql = primary.connect()
    psql.sql("SET client_min_messages TO WARNING")
    psql.sql("SELECT pg_backup_start('test label')")

    # Filesystem copy of the running data directory.
    backup_dir = tmp_path / "backup1"
    shutil.copytree(primary.datadir, backup_dir)

    # Remove files that should not be in the backup; pg_control is removed so it
    # can be copied last (with the advanced checkpoint), and pg_wal is emptied.
    (backup_dir / "postmaster.pid").unlink()
    (backup_dir / "postmaster.opts").unlink()
    (backup_dir / "global" / "pg_control").unlink()
    shutil.rmtree(backup_dir / "pg_wal")
    (backup_dir / "pg_wal").mkdir()

    # Data created after the backup started: used to tell which recovery path ran.
    primary.sql("create table canary (id int)")

    # Advance the checkpoint location in pg_control past the backup start, in a
    # new WAL segment, and make sure the switched-from segment is archived.
    segment_name = primary.sql("select pg_walfile_name(pg_switch_wal())")
    primary.poll_query_until(
        "SELECT last_archived_wal FROM pg_stat_archiver", expected=segment_name
    )
    primary.sql("checkpoint")

    # The segment holding the latest checkpoint record from pg_control; a later
    # recovery attempt removes it to hit the missing-checkpoint-record error.
    checkpoint_segment_name = primary.sql(
        "SELECT pg_walfile_name(checkpoint_lsn) FROM pg_control_checkpoint()"
    )

    # Copy pg_control last, so it carries the new checkpoint.
    shutil.copy(
        pathlib.Path(primary.datadir) / "global" / "pg_control",
        backup_dir / "global" / "pg_control",
    )

    # The segment that pg_backup_stop() archives; recovery without backup_label
    # will think it needs it, so it is provided in pg_wal below.
    stop_segment_name = primary.sql("SELECT pg_walfile_name(pg_current_wal_lsn())")
    backup_label = psql.sql("select labelfile from pg_backup_stop()")
    psql.close()

    # Recover without backup_label: recovery uses the advanced control file, so
    # it is crash recovery and the canary is missing (the cluster is corrupt).
    replica_fail = create_pg(
        "replica_fail",
        from_backup=backup_dir,
        conf={"archive_mode": False},
        start=False,
    )
    shutil.copy(
        primary.archive_dir / stop_segment_name,
        pathlib.Path(replica_fail.datadir) / "pg_wal" / stop_segment_name,
    )
    replica_fail.start()
    assert replica_fail.sql(CANARY_QUERY) == 0, "canary is missing"
    assert (
        "database system was not properly shut down; automatic recovery in progress"
        in replica_fail.log_since(0)
    ), "verify backup recovery performed with crash recovery"
    replica_fail.stop()

    # Recover with backup_label restored and the primary's archive: recovery
    # uses the correct redo location and the canary is present.
    # Write with newline="" so the exact "\n" line endings from pg_backup_stop()
    # are preserved; on Windows text mode would translate them to "\r\n", which
    # the server rejects as "invalid data in file backup_label".
    with open(backup_dir / "backup_label", "w", newline="") as f:
        f.write(backup_label)

    replica_success = create_pg(
        "replica_success", from_backup=backup_dir, restoring=primary
    )
    assert replica_success.sql(CANARY_QUERY) == 1, "canary is present"
    assert "starting backup recovery with redo LSN" in replica_success.log_since(0), (
        "verify backup recovery performed with backup_label"
    )
    replica_success.stop()

    # Recover with backup_label but the checkpoint segment removed: startup
    # fails because the checkpoint record cannot be found. Note that the backup
    # had its pg_wal/ wiped out previously, so the segment may already be gone.
    replica_missing = create_pg(
        "replica_missing_checkpoint",
        from_backup=backup_dir,
        conf={"archive_mode": False},
        start=False,
    )
    (pathlib.Path(replica_missing.datadir) / "pg_wal" / checkpoint_segment_name).unlink(
        missing_ok=True
    )

    with pytest.raises(subprocess.CalledProcessError):
        replica_missing.start()
    assert re.search(
        "FATAL: .*could not locate required checkpoint record at",
        replica_missing.log_since(0),
    ), "ends with FATAL for missing required checkpoint record"
