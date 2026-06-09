# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/004_timeline_switch.pl.

Tests timeline switches: a cascading standby must be able to follow a
newly-promoted standby onto its new timeline without its walreceiver being
restarted, and a standby must be able to follow a primary that was itself
promoted onto a newer timeline while WAL archiving is enabled.
"""


def test_timeline_switch(create_pg):
    # A cascading standby follows a newly-promoted standby.
    primary = create_pg("primary", allows_streaming=True)
    backup = primary.backup("my_backup")

    standby_1 = create_pg("standby_1", from_backup=backup, streaming_primary=primary)
    standby_2 = create_pg("standby_2", from_backup=backup, streaming_primary=primary)

    primary.sql("CREATE TABLE tab_int AS SELECT generate_series(1,1000) AS a")

    # Cleanly stop the primary so both standbys have received and flushed all
    # of its records, then promote standby 1 onto a new timeline.
    primary.stop()
    standby_1_conn = standby_1.connect()
    assert standby_1_conn.sql("SELECT pg_promote(wait_seconds => 300)") is True, (
        "promotion of standby with pg_promote"
    )

    # Switch standby 2 to replay from standby 1. The walreceiver must stay alive
    # across the timeline switch and the new connection string (which carries a
    # secret password) must not become visible in pg_stat_wal_receiver.
    secret = "dont_show_me"
    connstr_1 = standby_1.connstr()
    standby_2.append_conf(f"primary_conninfo='{connstr_1} password={secret}'")

    # A new log file is not used as it is in Perl, so capture the log position
    # before the restart and only inspect what is written afterwards.
    offset = standby_2.current_log_position()
    standby_2.pg_ctl("restart")
    standby_2_conn = standby_2.connect()  # the restart invalidated any connection

    # Wait for the walreceiver to reconnect after the restart, then record its
    # PID so we can confirm it survives the timeline switch.
    standby_2.poll_query_until("SELECT EXISTS(SELECT 1 FROM pg_stat_wal_receiver)")
    wr_pid_before = standby_2_conn.sql("SELECT pid FROM pg_stat_wal_receiver")

    standby_1_conn.sql("INSERT INTO tab_int VALUES (generate_series(1001,2000))")
    standby_1.wait_for_catchup(standby_2)

    assert standby_2_conn.sql("SELECT count(*) FROM tab_int") == 2000, (
        "check content of standby 2"
    )

    assert (
        "terminating walreceiver process due to administrator command"
        not in standby_2.log_since(offset)
    ), "WAL receiver should not be stopped across timeline jumps"

    wr_pid_after = standby_2_conn.sql("SELECT pid FROM pg_stat_wal_receiver")
    assert wr_pid_before == wr_pid_after, (
        "WAL receiver PID matches across timeline jumps"
    )

    assert (
        standby_2_conn.sql(
            "SELECT count(*) FROM pg_stat_wal_receiver "
            f"WHERE conninfo LIKE '%{secret}%'"
        )
        == 0
    ), "pg_stat_wal_receiver.conninfo not updated across timeline jumps"

    # A standby follows a primary promoted onto a newer timeline, with archiving.
    primary_2 = create_pg(
        "primary_2", allows_streaming=True, archiving=True,
        conf=["wal_keep_size = 512MB"],
    )
    backup_2 = primary_2.backup("my_backup")

    standby_3 = create_pg(
        "standby_3", from_backup=backup_2, streaming_primary=primary_2, start=False
    )

    # Restart primary 2 in standby mode and promote it onto a new timeline.
    primary_2.append_conf(filename="standby.signal")
    primary_2.pg_ctl("restart")
    primary_2.promote()

    standby_3.start()
    primary_2.sql("CREATE TABLE tab_int AS SELECT 1 AS a")
    primary_2.wait_for_catchup(standby_3)

    assert standby_3.sql("SELECT count(*) FROM tab_int") == 1, (
        "check content of standby 3"
    )
