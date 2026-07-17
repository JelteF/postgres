# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/002_archiving.pl.

Tests WAL archiving with a hot standby that restores from the primary's archive:
content reaches the standby through the archive, archive_cleanup_command and
recovery_end_command fire at the expected times, and the archive-recovery
temporary files (RECOVERYHISTORY/RECOVERYXLOG) plus both signal files are
cleaned up at promotion.
"""

import re


def test_archiving(create_pg):
    primary = create_pg("primary", archiving=True, allows_streaming=True)
    backup = primary.backup("my_backup")

    # A standby that fetches WAL from the primary's archive. Its
    # archive_cleanup_command and recovery_end_command write marker files into
    # the data directory; configure them before the first start.
    cleanup_file = "archive_cleanup_command.done"
    end_file = "recovery_end_command.done"
    standby = create_pg(
        "standby",
        from_backup=backup,
        restoring=primary,
        conf={
            "wal_retrieve_retry_interval": "100ms",
            "archive_cleanup_command": f"echo done > {cleanup_file}",
            "recovery_end_command": f"echo done > {end_file}",
        },
    )

    primary.sql("CREATE TABLE tab_int AS SELECT generate_series(1,1000) AS a")
    # Checkpoint before the segment switch, so it is replayed on the standby for
    # the archive_cleanup_command check below.
    primary.sql("CHECKPOINT")
    current_lsn = primary.lsn("write")
    # Force archiving of the current WAL file.
    primary.sql("SELECT pg_switch_wal()")
    # This extra content should not reach the standby (it is past current_lsn).
    primary.sql("INSERT INTO tab_int VALUES (generate_series(1001,2000))")

    standby.poll_query_until(
        f"SELECT '{current_lsn}'::pg_lsn <= pg_last_wal_replay_lsn()"
    )
    assert standby.sql("SELECT count(*) FROM tab_int") == 1000, (
        "check content from archives"
    )

    # archive_cleanup_command runs when a restartpoint (checkpoint) is created.
    standby.sql("CHECKPOINT")
    assert (standby.datadir / cleanup_file).is_file(), (
        "archive_cleanup_command executed on checkpoint"
    )
    assert not (standby.datadir / end_file).is_file(), (
        "recovery_end_command not executed yet"
    )

    # Promotion runs recovery_end_command and bumps the timeline; the new
    # 00000002.history is archived to the primary's archive.
    standby.promote()
    primary.poll_query_until(
        "SELECT size IS NOT NULL FROM "
        f"pg_stat_file('{primary.archive_dir}/00000002.history', true)"
    )
    assert (standby.datadir / end_file).is_file(), (
        "recovery_end_command executed after promotion"
    )

    # A second standby from the same backup, used to exercise the archive
    # history file (RECOVERYHISTORY) and signal-file cleanup at promotion. It is
    # built with start=False so recovery.signal can be added on top of the
    # standby.signal before starting, and recovery_end_command is made to fail.
    standby2 = create_pg(
        "standby2",
        from_backup=backup,
        restoring=primary,
        start=False,
        conf={"recovery_end_command": "echo failed > missing_dir/xyz.file"},
    )
    (standby2.datadir / "recovery.signal").touch()
    assert (standby2.datadir / "recovery.signal").is_file()
    assert (standby2.datadir / "standby.signal").is_file()

    standby2.start()
    log_offset = standby2.current_log_position()
    standby2.promote()

    log = standby2.log_since(log_offset)
    assert re.search(r'restored log file "00000002.history" from archive', log), (
        "00000002.history retrieved from the archives"
    )
    assert not (standby2.datadir / "pg_wal" / "RECOVERYHISTORY").is_file(), (
        "RECOVERYHISTORY removed after promotion"
    )
    assert not (standby2.datadir / "pg_wal" / "RECOVERYXLOG").is_file(), (
        "RECOVERYXLOG removed after promotion"
    )
    assert re.search(r"WARNING:.*recovery_end_command", log, re.S), (
        "recovery_end_command failure detected in logs after promotion"
    )
    # Both signal files removed at the end of recovery.
    assert not (standby2.datadir / "recovery.signal").is_file()
    assert not (standby2.datadir / "standby.signal").is_file()
