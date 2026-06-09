# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/020_archive_status.pl.

Tests WAL archive-status (.ready/.done) handling: that a failing
archive_command leaves a .ready file and is reported in pg_stat_archiver, that
crash recovery does not remove non-archived segments, that a successful archive
swaps .ready for .done, and how a standby manages status files under
archive_mode = on versus always. Also checks the shell archive module's
shutdown callback and that backup mode can be entered/left without crashes.
"""

import pathlib

import pytest

from libpq import LibpqError

# A copy command that always fails (the source path does not exist), used to
# make the archiver fail while archiving is enabled.
FAILING_ARCHIVE_COMMAND = 'cp "%p_does_not_exist" "%f_does_not_exist"'


def test_archive_status(create_pg):
    primary = create_pg(
        "primary", archiving=True, allows_streaming=True, conf={"autovacuum": False}
    )
    primary_data = pathlib.Path(primary.datadir)

    # Make the archiver fail while archiving is still enabled.
    primary.sql(f"ALTER SYSTEM SET archive_command TO '{FAILING_ARCHIVE_COMMAND}'")
    primary.sql("SELECT pg_reload_conf()")

    # Remember the current segment and switch away from it.
    segment_1 = primary.sql("SELECT pg_walfile_name(pg_current_wal_lsn())")
    seg1_ready = primary_data / "pg_wal/archive_status" / f"{segment_1}.ready"
    seg1_done = primary_data / "pg_wal/archive_status" / f"{segment_1}.done"
    primary.sql("CREATE TABLE mine AS SELECT generate_series(1,10) AS x")
    primary.sql("SELECT pg_switch_wal()")
    primary.sql("CHECKPOINT")

    primary.poll_query_until("SELECT failed_count > 0 FROM pg_stat_archiver")
    assert seg1_ready.is_file(), (
        f".ready file exists for WAL segment {segment_1} waiting to be archived"
    )
    assert not seg1_done.is_file(), (
        f".done file does not exist for WAL segment {segment_1} waiting to be archived"
    )
    assert primary.sql(
        "SELECT archived_count, last_failed_wal FROM pg_stat_archiver"
    ) == (0, segment_1), f"pg_stat_archiver failed to archive {segment_1}"

    # Crash, take a cold backup (with the failing archive_command), restart.
    primary.stop("immediate")
    backup = primary.backup_fs_cold("backup")
    primary.start()
    assert seg1_ready.is_file(), (
        f".ready file for WAL segment {segment_1} still exists after crash recovery"
    )

    # Allow archiving again and wait for success.
    primary.sql("ALTER SYSTEM RESET archive_command")
    primary.sql("SELECT pg_reload_conf()")
    primary.poll_query_until("SELECT archived_count FROM pg_stat_archiver", expected=1)
    assert not seg1_ready.is_file(), (
        f".ready file for archived WAL segment {segment_1} removed"
    )
    assert seg1_done.is_file(), (
        f".done file for archived WAL segment {segment_1} exists"
    )
    assert primary.sql("SELECT last_archived_wal FROM pg_stat_archiver") == segment_1, (
        f"archive success reported in pg_stat_archiver for WAL segment {segment_1}"
    )

    # More activity and a checkpoint so the next standby can create a clean
    # restartpoint (it starts in crash recovery from the cold backup).
    segment_2 = primary.sql("SELECT pg_walfile_name(pg_current_wal_lsn())")
    seg2_ready_name = f"{segment_2}.ready"
    seg2_done_name = f"{segment_2}.done"
    primary.sql("INSERT INTO mine SELECT generate_series(10,20) AS x")
    primary.sql("CHECKPOINT")
    primary_lsn = primary.sql("SELECT pg_switch_wal()")
    primary.poll_query_until(
        "SELECT last_archived_wal FROM pg_stat_archiver", expected=segment_2
    )

    # Standby with archive_mode = on.
    standby1 = create_pg(
        "standby", from_backup=backup, restoring=primary, conf={"archive_mode": "on"}
    )
    standby1_data = pathlib.Path(standby1.datadir)
    standby1.poll_query_until(
        f"SELECT pg_wal_lsn_diff(pg_last_wal_replay_lsn(), '{primary_lsn}') >= 0"
    )
    standby1.sql("CHECKPOINT")

    # archive_mode=on drops inherited .ready files and creates no new ones, but
    # does create .done files.
    assert not (
        standby1_data / "pg_wal/archive_status" / f"{segment_1}.ready"
    ).is_file(), (
        f".ready file for segment {segment_1} from backup removed with archive_mode=on"
    )
    assert not (standby1_data / "pg_wal/archive_status" / seg2_ready_name).is_file(), (
        f".ready file for segment {segment_2} not created with archive_mode=on on standby"
    )
    assert (standby1_data / "pg_wal/archive_status" / seg2_done_name).is_file(), (
        f".done file for segment {segment_2} created with archive_mode=on on standby"
    )

    # Standby with archive_mode = always keeps/creates .ready files (it inherits
    # the failing archive_command from the cold backup).
    standby2 = create_pg(
        "standby2",
        from_backup=backup,
        restoring=primary,
        conf={"archive_mode": "always"},
    )
    standby2_data = pathlib.Path(standby2.datadir)
    seg1_ready2 = standby2_data / "pg_wal/archive_status" / f"{segment_1}.ready"
    seg2_ready2 = standby2_data / "pg_wal/archive_status" / seg2_ready_name
    seg1_done2 = standby2_data / "pg_wal/archive_status" / f"{segment_1}.done"
    seg2_done2 = standby2_data / "pg_wal/archive_status" / seg2_done_name
    standby2.poll_query_until(
        f"SELECT pg_wal_lsn_diff(pg_last_wal_replay_lsn(), '{primary_lsn}') >= 0"
    )
    standby2.sql("CHECKPOINT")
    assert seg1_ready2.is_file(), (
        f".ready file for segment {segment_1} in backup kept with archive_mode=always"
    )
    assert seg2_ready2.is_file(), (
        f".ready file for segment {segment_2} created with archive_mode=always"
    )

    standby2.sql("SELECT pg_stat_reset_shared('archiver')")

    # Crash recovery must not remove non-archived segments on the standby.
    standby2.stop("immediate")
    standby2.start()
    assert seg1_ready2.is_file(), (
        "WAL segment still ready to archive after crash recovery with archive_mode=always"
    )

    # Allow archiving and wait for both segments to be archived.
    standby2.sql("ALTER SYSTEM RESET archive_command")
    standby2.sql("SELECT pg_reload_conf()")
    standby2.poll_query_until(
        "SELECT last_archived_wal FROM pg_stat_archiver", expected=segment_2
    )
    assert standby2.sql("SELECT archived_count FROM pg_stat_archiver") == 2, (
        "correct number of WAL segments archived from standby"
    )
    assert not seg1_ready2.is_file() and not seg2_ready2.is_file(), (
        ".ready files removed after archive success with archive_mode=always on standby"
    )
    assert seg1_done2.is_file() and seg2_done2.is_file(), (
        ".done files created after archive success with archive_mode=always on standby"
    )

    # The shell archive module's shutdown callback runs on archiver shutdown.
    standby2.append_conf(log_min_messages="debug1")
    standby2.pg_ctl("reload")
    standby2.sql("SELECT 1")  # ensure the reload took effect
    log_offset = standby2.current_log_position()
    standby2.stop()
    assert "archiver process shutting down" in standby2.log_since(log_offset), (
        "check shutdown callback of shell archive module"
    )

    # Backup mode can be entered and left without crashes; a too-long label
    # fails gracefully.
    with pytest.raises(LibpqError, match="backup label too long"):
        primary.sql_batch(
            "SELECT pg_backup_start('onebackup')",
            "SELECT pg_backup_stop()",
            "SELECT pg_backup_start(repeat('x', 1026))",
        )
    primary.sql_batch("SELECT pg_backup_start('onebackup')", "SELECT pg_backup_stop()")
    primary.sql("SELECT pg_backup_start('twobackup')")
