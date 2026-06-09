# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/042_low_level_backup.pl.

Tests the low-level backup method using pg_backup_start()/pg_backup_stop() with
a manual filesystem copy. Recovering the copy without backup_label must fall
back to crash recovery from the (advanced) control file and miss data created
after the backup started; recovering with backup_label restored into place uses
the correct redo location and sees that data.
"""

import pathlib
import shutil

CANARY_QUERY = "select count(*) from pg_class where relname = 'canary'"


def test_low_level_backup(create_pg, tmp_path):
    primary = create_pg("primary", archiving=True, allows_streaming=True)

    # Hold the non-exclusive backup open on one session while copying files.
    psql = primary.background()
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

    conn = primary.connect()
    # Data created after the backup started: used to tell which recovery path ran.
    conn.sql("create table canary (id int)")

    # Advance the checkpoint location in pg_control past the backup start, in a
    # new WAL segment, and make sure the switched-from segment is archived.
    segment_name = conn.sql("select pg_walfile_name(pg_switch_wal())")
    primary.poll_query_until(
        "SELECT last_archived_wal FROM pg_stat_archiver", segment_name
    )
    conn.sql("checkpoint")

    # Copy pg_control last, so it carries the new checkpoint.
    shutil.copy(
        pathlib.Path(primary.datadir) / "global" / "pg_control",
        backup_dir / "global" / "pg_control",
    )

    # The segment that pg_backup_stop() archives; recovery without backup_label
    # will think it needs it, so it is provided in pg_wal below.
    stop_segment_name = conn.sql("SELECT pg_walfile_name(pg_current_wal_lsn())")
    backup_label = psql.sql("select labelfile from pg_backup_stop()")
    psql.quit()

    # Recover without backup_label: recovery uses the advanced control file, so
    # it is crash recovery and the canary is missing (the cluster is corrupt).
    replica_fail = create_pg(
        "replica_fail", from_backup=backup_dir, conf=["archive_mode = off"], start=False
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
    with open(backup_dir / "backup_label", "w") as f:
        f.write(backup_label)

    replica_success = create_pg(
        "replica_success", from_backup=backup_dir, restoring=primary
    )
    assert replica_success.sql(CANARY_QUERY) == 1, "canary is present"
    assert "starting backup recovery with redo LSN" in replica_success.log_since(0), (
        "verify backup recovery performed with backup_label"
    )
