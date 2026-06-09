# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/025_stuck_on_old_timeline.pl.

Tests that a cascading standby with no local WAL can follow a newly-promoted
standby. The archive_command only copies timeline history files (never WAL
segments), simulating the race where the cascading standby starts after the
history file reaches the archive but before any WAL does, so all WAL must be
streamed rather than restored.
"""


def test_stuck_on_old_timeline(create_pg):
    primary = create_pg("primary", allows_streaming=True, archiving=True)

    # Archive only history files, never WAL segments. No real archive_command
    # behaves this way; it forces the cascading standby to stream all WAL.
    archive_cmd = (
        f'case "%f" in *.history) cp "%p" "{primary.archive_dir}/%f" ;; '
        f"*) exit 0 ;; esac"
    )
    pconn = primary.connect()
    pconn.sql(f"ALTER SYSTEM SET archive_command = '{archive_cmd}'")
    pconn.sql("ALTER SYSTEM SET wal_keep_size = '128MB'")
    pconn.sql("SELECT pg_reload_conf()")

    backup = primary.backup("my_backup")

    # Streaming standby of the primary (inherits the history-only archive_command).
    standby = create_pg("standby", from_backup=backup, streaming_primary=primary)

    # Backup of the standby with empty pg_wal, for the cascading standby.
    cascade_backup = standby.backup("my_backup", backup_options=["-Xnone"])

    # Cascading standby: streams from the standby and restores from the
    # primary's archive (which only ever holds history files). Not started yet.
    cascade = create_pg(
        "cascade",
        from_backup=cascade_backup,
        streaming_primary=standby,
        restoring=primary,
        conf=["recovery_target_timeline='latest'"],
        start=False,
    )

    standby.promote()
    standby.poll_query_until("SELECT NOT pg_is_in_recovery()")
    sconn = standby.connect()

    # Switch WAL and wait until the new segment is archived; since the history
    # file is created and archived on promotion before any WAL segment, this
    # guarantees the history file has reached the archive.
    walfile = sconn.sql("SELECT pg_walfile_name(pg_current_wal_lsn())")
    sconn.sql("SELECT pg_switch_wal()")
    standby.poll_query_until(
        f"SELECT '{walfile}' <= last_archived_wal FROM pg_stat_archiver"
    )

    cascade.start()

    sconn.sql("CREATE TABLE tab_int AS SELECT 1 AS a")
    standby.wait_for_catchup(cascade)
    assert cascade.sql("SELECT count(*) FROM tab_int") == 1, (
        "check streamed content on cascade standby"
    )
