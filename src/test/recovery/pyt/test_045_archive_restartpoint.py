# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/045_archive_restartpoint.pl.

Tests that restartpoints happen during archive recovery: a primary generates
many small WAL segments, and a node recovers from the archive up to a target
LSN (pausing there) with a small max_wal_size so restartpoints are forced.
"""

ARCHIVE_MAX_MB = 320
WAL_SEGSIZE = 1


def test_archive_restartpoint(create_pg):
    primary = create_pg(
        "primary",
        archiving=True,
        allows_streaming=True,
        initdb_opts=["--wal-segsize", str(WAL_SEGSIZE)],
    )
    backup = primary.backup("my_backup")

    iterations = ARCHIVE_MAX_MB // WAL_SEGSIZE
    primary.sql(
        f"DO $$BEGIN FOR i IN 1..{iterations} LOOP CHECKPOINT; "
        "PERFORM pg_switch_wal(); END LOOP; END$$;"
    )

    # Force archiving of the WAL file containing the recovery target.
    until_lsn = primary.lsn("write")
    primary.sql("SELECT pg_switch_wal()")
    primary.stop()

    # Recover from the archive up to until_lsn, pausing there. A small
    # max_wal_size forces restartpoints during recovery.
    restore = create_pg(
        "restore",
        from_backup=backup,
        restoring=primary,
        conf={
            "recovery_target_lsn": until_lsn,
            "recovery_target_action": "pause",
            "max_wal_size": 2 * WAL_SEGSIZE,
            "log_checkpoints": True,
        },
    )

    restore.poll_query_until(
        f"SELECT '{until_lsn}'::pg_lsn <= pg_last_wal_replay_lsn()"
    )
    restore.stop()
