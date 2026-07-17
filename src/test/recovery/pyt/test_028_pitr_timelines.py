# Copyright (c) 2022-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/028_pitr_timelines.pl.

Tests point-in-time recovery to a target that lies physically in a WAL segment
belonging to a higher timeline. A standby is promoted (creating timeline 2) and
archives a segment whose first half holds timeline-1 WAL up to the switch.
PITR to a restore point in that first half must find the WAL there but not
follow the timeline switch, producing a 1 -> 3 end-of-recovery record; a second
PITR node then recovers across that switch.

All nodes share the primary's archive: the standby and PITR nodes inherit the
primary's archive_command (pointing at the primary's archive directory) from
the base backup, and the PITR nodes restore from that same directory.
"""


def test_pitr_timelines(create_pg):
    primary = create_pg("primary", archiving=True, allows_streaming=True)
    backup = primary.backup("my_backup")

    # Workload plus the restore point. These run as separate autocommit
    # statements so 'rp' falls between the two inserts' commits.
    primary.sql("CREATE TABLE foo(i int)")
    primary.sql("INSERT INTO foo VALUES(1)")
    primary.sql("SELECT pg_create_restore_point('rp')")
    primary.sql("INSERT INTO foo VALUES(2)")

    # Standby that also archives (archive_mode=always, archiving to the
    # primary's archive directory inherited from the backup).
    standby = create_pg(
        "standby",
        from_backup=backup,
        streaming_primary=primary,
        conf={"archive_mode": "always"},
    )
    primary.wait_for_catchup(standby)
    assert standby.sql("SELECT max(i) FROM foo") == 2, (
        "check table contents after archive recovery"
    )

    # Kill the primary before it archives the segment with the INSERTs.
    primary.stop("immediate")

    # Promote the standby (timeline 2) and switch WAL so it archives a segment
    # on the new timeline that contains the timeline-1 WAL up to the switch.
    standby.promote()
    standby.sql("SELECT pg_switch_wal()")
    # Shutting down finishes archiving all timeline-2 WAL.
    standby.stop()

    # PITR to the restore point: should find the WAL in the timeline-2 segment
    # but not follow the timeline switch, giving timeline 3.
    node_pitr = create_pg(
        "node_pitr",
        from_backup=backup,
        restoring=primary,
        restoring_standby=False,
        conf={"recovery_target_name": "rp", "recovery_target_action": "promote"},
    )
    node_pitr.poll_query_until("SELECT pg_is_in_recovery() = 'f'")
    assert node_pitr.sql("SELECT max(i) FROM foo") == 1, (
        "check table contents after point-in-time recovery"
    )

    # A row on the new timeline, to confirm it is recovered later.
    node_pitr.sql("INSERT INTO foo VALUES(3)")

    # Wait for the archiver to be running before stopping, so the last segment
    # is archived.
    node_pitr.poll_query_until(
        "SELECT true FROM pg_stat_activity WHERE backend_type = 'archiver'"
    )
    node_pitr.stop()

    # Archive recovery on the PITR-created timeline replays the 1 -> 3
    # end-of-recovery switch.
    node_pitr2 = create_pg(
        "node_pitr2",
        from_backup=backup,
        restoring=primary,
        restoring_standby=False,
        conf={"recovery_target_action": "promote"},
    )
    node_pitr2.poll_query_until("SELECT pg_is_in_recovery() = 'f'")
    assert node_pitr2.sql("SELECT max(i) FROM foo") == 3, (
        "check table contents after point-in-time recovery"
    )
