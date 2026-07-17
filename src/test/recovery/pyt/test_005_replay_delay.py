# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/005_replay_delay.pl.

Checks recovery_min_apply_delay (a standby applies WAL only after the configured
delay) and recovery pause/resume (a paused standby streams but does not replay
WAL, and a promotion ends the paused state).
"""

import time

DELAY = 3


def test_replay_delay(create_pg):
    primary = create_pg("primary", allows_streaming=True)
    primary.sql("CREATE TABLE tab_int AS SELECT generate_series(1, 10) AS a")
    backup = primary.backup("my_backup")

    # A standby that delays applying WAL by DELAY seconds.
    standby = create_pg(
        "standby",
        from_backup=backup,
        streaming_primary=primary,
        conf={"recovery_min_apply_delay": f"{DELAY}s"},
    )

    # Timestamp before the insert, as the comparison base for the delay.
    primary_insert_time = time.time()
    primary.sql("INSERT INTO tab_int VALUES (generate_series(11, 20))")
    until_lsn = primary.lsn("write")

    standby.poll_query_until(
        f"SELECT (pg_last_wal_replay_lsn() - '{until_lsn}'::pg_lsn) >= 0"
    )
    assert time.time() - primary_insert_time >= DELAY, (
        "standby applies WAL only after replication delay"
    )

    # A second standby to exercise recovery pause/resume.
    standby2 = create_pg("standby2", from_backup=backup, streaming_primary=primary)

    assert standby2.sql("SELECT pg_get_wal_replay_pause_state()") == "not paused", (
        "pg_get_wal_replay_pause_state() reports not paused"
    )

    # Pause recovery and wait until it is actually paused.
    standby2.sql("SELECT pg_wal_replay_pause()")
    primary.sql("INSERT INTO tab_int VALUES (generate_series(21,30))")
    standby2.poll_query_until("SELECT pg_get_wal_replay_pause_state() = 'paused'")

    # New WAL streams in, but the paused standby does not replay it.
    receive_lsn = standby2.lsn("receive")
    replay_lsn = standby2.lsn("replay")
    primary.sql("INSERT INTO tab_int VALUES (generate_series(31,40))")
    standby2.poll_query_until(
        f"SELECT '{receive_lsn}'::pg_lsn < pg_last_wal_receive_lsn()"
    )
    assert standby2.lsn("replay") == replay_lsn, (
        "no WAL is replayed in the paused state"
    )

    # Resume recovery and confirm replay advances past the paused LSN.
    standby2.sql("SELECT pg_wal_replay_resume()")
    standby2.poll_query_until(
        "SELECT pg_get_wal_replay_pause_state() = 'not paused' "
        f"AND pg_last_wal_replay_lsn() > '{replay_lsn}'::pg_lsn"
    )

    # A promotion triggered while paused ends the paused state and continues.
    standby2.sql("SELECT pg_wal_replay_pause()")
    primary.sql("INSERT INTO tab_int VALUES (generate_series(41,50))")
    standby2.poll_query_until("SELECT pg_get_wal_replay_pause_state() = 'paused'")

    standby2.promote()
    standby2.poll_query_until("SELECT NOT pg_is_in_recovery()")
