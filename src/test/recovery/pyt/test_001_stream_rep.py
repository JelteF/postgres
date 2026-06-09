# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/001_stream_rep.pl.

Streaming replication: a cascade of primary -> standby_1 -> standby_2, content
and sequence replication, read-only enforcement on standbys, the
target_session_attrs connection parameter, SHOW/READ_REPLICATION_SLOT over a
replication connection, switching to physical replication slots and observing
slot xmins with and without hot_standby_feedback, physical slot advancing
durability with WAL recycling, and BASE_BACKUP error/cancellation handling.
"""

from libpq import LibpqError
import pytest


def test_stream_rep(create_pg):
    primary = create_pg("primary", allows_streaming=True)
    backup = primary.backup("my_backup")

    standby_1 = create_pg("standby_1", from_backup=backup, streaming_primary=primary)

    # Take a backup of standby 1 (useful to check pg_basebackup works on a
    # standby), then a second one while the primary is offline.
    backup_s1 = standby_1.backup("my_backup")
    primary.stop()
    standby_1.backup("my_backup_2")
    primary.start()

    # Second standby cascades from standby 1.
    standby_2 = create_pg("standby_2", from_backup=backup_s1, streaming_primary=standby_1)

    def catchup():
        # Cascade catchup keyed off the primary's flush LSN, since a standby
        # can't report its own write LSN with pg_current_wal_lsn().
        lsn = primary.lsn("flush")
        primary.wait_for_catchup(standby_1, "replay", lsn)
        standby_1.wait_for_catchup(standby_2, "replay", lsn)

    # Reset IO statistics, for the WAL sender check with pg_stat_io.
    primary.sql("SELECT pg_stat_reset_shared('io')")

    # Create some content on the primary and check its presence on the standbys.
    primary.sql("CREATE TABLE tab_int AS SELECT generate_series(1,1002) AS a")

    # A login event trigger that records logins (skipped during recovery) and
    # rejects a particular role.
    primary.sql(
        """
        CREATE TABLE user_logins(id serial, who text);

        CREATE FUNCTION on_login_proc() RETURNS EVENT_TRIGGER AS $$
        BEGIN
          IF NOT pg_is_in_recovery() THEN
            INSERT INTO user_logins (who) VALUES (session_user);
          END IF;
          IF session_user = 'regress_hacker' THEN
            RAISE EXCEPTION 'You are not welcome!';
          END IF;
        END;
        $$ LANGUAGE plpgsql SECURITY DEFINER;

        CREATE EVENT TRIGGER on_login_trigger ON login EXECUTE FUNCTION on_login_proc();
        ALTER EVENT TRIGGER on_login_trigger ENABLE ALWAYS;
        """
    )
    catchup()

    assert standby_1.sql("SELECT count(*) FROM tab_int") == 1002, (
        "check streamed content on standby 1"
    )
    assert standby_2.sql("SELECT count(*) FROM tab_int") == 1002, (
        "check streamed content on standby 2"
    )
    assert (
        standby_1.sql(
            "SELECT count(*) FROM pg_stat_recovery WHERE promote_triggered IS NOT NULL"
        )
        == 1
    ), "check recovery state on standby 1"

    # Likewise for a sequence.
    primary.sql("CREATE SEQUENCE seq1; SELECT nextval('seq1')")
    catchup()
    assert standby_1.sql("SELECT * FROM seq1") == (33, 0, True), (
        "check streamed sequence content on standby 1"
    )
    assert standby_2.sql("SELECT * FROM seq1") == (33, 0, True), (
        "check streamed sequence content on standby 2"
    )

    # pg_sequence_last_value() returns NULL for an unlogged sequence on a standby.
    primary.sql("CREATE UNLOGGED SEQUENCE ulseq; SELECT nextval('ulseq')")
    primary.wait_for_catchup(standby_1, "replay", primary.lsn("flush"))
    assert (
        standby_1.sql("SELECT pg_sequence_last_value('ulseq'::regclass) IS NULL") is True
    ), "pg_sequence_last_value() on unlogged sequence on standby 1"

    # Only read-only queries can run on standbys.
    for node in (standby_1, standby_2):
        with pytest.raises(LibpqError, match="cannot execute INSERT in a read-only transaction"):
            node.sql("INSERT INTO tab_int VALUES (1)")

    # target_session_attrs: attempt to connect to node1 then node2 with the
    # given mode; expect to land on target (None means the attempt must fail).
    def check_tsa(node1, node2, target, mode):
        host = f"{node1.host},{node2.host}"
        port = f"{node1.port},{node2.port}"
        if target is None:
            with pytest.raises(LibpqError):
                node1.connect(
                    host=host, port=port, target_session_attrs=mode, dbname="postgres"
                )
        else:
            with node1.connect(
                host=host, port=port, target_session_attrs=mode, dbname="postgres"
            ) as c:
                assert c.sql("SHOW port") == str(target.port), (
                    f"connect with mode {mode}"
                )

    check_tsa(primary, standby_1, primary, "read-write")
    check_tsa(standby_1, primary, primary, "read-write")
    check_tsa(primary, standby_1, primary, "any")
    check_tsa(standby_1, primary, standby_1, "any")
    check_tsa(primary, standby_1, primary, "primary")
    check_tsa(standby_1, primary, primary, "primary")
    check_tsa(primary, standby_1, standby_1, "read-only")
    check_tsa(standby_1, primary, standby_1, "read-only")
    check_tsa(primary, primary, primary, "prefer-standby")
    check_tsa(primary, standby_1, standby_1, "prefer-standby")
    check_tsa(standby_1, primary, standby_1, "prefer-standby")
    check_tsa(primary, standby_1, standby_1, "standby")
    check_tsa(standby_1, primary, standby_1, "standby")
    check_tsa(standby_1, standby_2, None, "read-write")
    check_tsa(standby_1, standby_2, None, "primary")
    check_tsa(primary, primary, None, "read-only")
    check_tsa(primary, primary, None, "standby")

    # SHOW commands over a replication connection with a replication role.
    primary.sql("CREATE ROLE repl_role REPLICATION LOGIN; GRANT pg_read_all_settings TO repl_role")
    for repl in ("1", "database"):
        with primary.connect(user="repl_role", dbname="postgres", replication=repl) as c:
            c.sql("SHOW ALL")
            c.sql("SHOW work_mem")
            c.sql("SHOW primary_conninfo")

    # READ_REPLICATION_SLOT over a replication connection.
    slotname = "test_read_replication_slot_physical"
    with primary.connect(user="repl_role", dbname="postgres", replication="1") as c:
        assert c.sql("READ_REPLICATION_SLOT non_existent_slot") == (None, None, None), (
            "READ_REPLICATION_SLOT returns NULL values if slot does not exist"
        )
        c.sql(f"CREATE_REPLICATION_SLOT {slotname} PHYSICAL RESERVE_WAL")
        info = c.sql(f"READ_REPLICATION_SLOT {slotname}")
        assert info[0] == "physical" and info[2] == 1, (
            "READ_REPLICATION_SLOT returns tuple with slot information"
        )
        c.sql(f"DROP_REPLICATION_SLOT {slotname}")

    # Wait for the physical WAL sender to update its IO statistics before the
    # next restart (which would force a flush of its stats).
    primary.poll_query_until(
        "SELECT sum(reads) > 0 FROM pg_catalog.pg_stat_io "
        "WHERE backend_type = 'walsender' AND object = 'wal'"
    )

    # Switch to physical replication slots on both standbys. No new backup is
    # needed since physical slots can go backwards. Speed up standby feedback.
    primary.append_conf("max_replication_slots = 4")
    primary.pg_ctl("restart")
    primary.sql("SELECT pg_create_physical_replication_slot('standby_1')")
    standby_1.append_conf(
        "primary_slot_name = standby_1",
        "wal_receiver_status_interval = 1",
        "max_replication_slots = 4",
    )
    standby_1.pg_ctl("restart")
    standby_1.sql("SELECT pg_create_physical_replication_slot('standby_2')")
    standby_2.append_conf(
        "primary_slot_name = standby_2", "wal_receiver_status_interval = 1"
    )
    # primary_slot_name can change without a restart.
    standby_2.pg_ctl("reload")

    def get_slot_xmins(node, slotname, check_expr):
        # Wait for the slot to reach a quiescent state, then return its xmin and
        # catalog_xmin (as text, or None when NULL).
        node.poll_query_until(
            f"SELECT {check_expr} FROM pg_catalog.pg_replication_slots "
            f"WHERE slot_name = '{slotname}'"
        )
        return node.sql(
            "SELECT xmin::text, catalog_xmin::text FROM pg_catalog.pg_replication_slots "
            f"WHERE slot_name = '{slotname}'"
        )

    # No hot standby feedback and no logical slots, so both slots' xmins are null.
    xmin, catalog_xmin = get_slot_xmins(
        primary, "standby_1", "xmin IS NULL AND catalog_xmin IS NULL"
    )
    assert xmin is None, "xmin of non-cascaded slot null with no hs_feedback"
    assert catalog_xmin is None, "catalog xmin of non-cascaded slot null with no hs_feedback"

    xmin, catalog_xmin = get_slot_xmins(
        standby_1, "standby_2", "xmin IS NULL AND catalog_xmin IS NULL"
    )
    assert xmin is None, "xmin of cascaded slot null with no hs_feedback"
    assert catalog_xmin is None, "catalog xmin of cascaded slot null with no hs_feedback"

    primary.sql("CREATE TABLE replayed(val integer)")

    def replay_check():
        newval = primary.sql(
            "INSERT INTO replayed(val) SELECT coalesce(max(val),0) + 1 AS newval "
            "FROM replayed RETURNING val"
        )
        catchup()
        assert standby_1.sql(f"SELECT 1 FROM replayed WHERE val = {newval}") == 1, (
            f"standby_1 didn't replay primary value {newval}"
        )
        assert standby_2.sql(f"SELECT 1 FROM replayed WHERE val = {newval}") == 1, (
            f"standby_2 didn't replay standby_1 value {newval}"
        )

    replay_check()

    assert (
        standby_1.sql("SELECT evtname FROM pg_event_trigger WHERE evtevent = 'login'")
        == "on_login_trigger"
    ), "Name of login trigger"
    assert (
        standby_2.sql("SELECT evtname FROM pg_event_trigger WHERE evtevent = 'login'")
        == "on_login_trigger"
    ), "Name of login trigger"

    # Enable hot_standby_feedback. The slots should gain an xmin.
    standby_1.sql("ALTER SYSTEM SET hot_standby_feedback = on")
    standby_1.pg_ctl("reload")
    standby_2.sql("ALTER SYSTEM SET hot_standby_feedback = on")
    standby_2.pg_ctl("reload")
    replay_check()

    xmin, catalog_xmin = get_slot_xmins(
        primary, "standby_1", "xmin IS NOT NULL AND catalog_xmin IS NULL"
    )
    assert xmin is not None, "xmin of non-cascaded slot non-null with hs feedback"
    assert catalog_xmin is None, "catalog xmin of non-cascaded slot still null with hs_feedback"

    xmin1, catalog_xmin1 = get_slot_xmins(
        standby_1, "standby_2", "xmin IS NOT NULL AND catalog_xmin IS NULL"
    )
    assert xmin1 is not None, "xmin of cascaded slot non-null with hs feedback"
    assert catalog_xmin1 is None, "catalog xmin of cascaded slot still null with hs_feedback"

    # Do some work to advance xmin (each iteration consumes an XID).
    primary.sql(
        """
        do $$
        begin
          for i in 10000..11000 loop
            begin
              insert into tab_int values (i);
            exception
              when division_by_zero then null;
            end;
          end loop;
        end$$;
        """
    )
    primary.sql("VACUUM")
    primary.sql("CHECKPOINT")

    xmin2, catalog_xmin2 = get_slot_xmins(primary, "standby_1", f"xmin <> '{xmin}'")
    assert xmin2 != xmin, "xmin of non-cascaded slot with hs feedback has changed"
    assert catalog_xmin2 is None, "catalog xmin of non-cascaded slot still null"

    xmin2, catalog_xmin2 = get_slot_xmins(standby_1, "standby_2", f"xmin <> '{xmin1}'")
    assert xmin2 != xmin1, "xmin of cascaded slot with hs feedback has changed"
    assert catalog_xmin2 is None, "catalog xmin of cascaded slot still null"

    # Disable hot_standby_feedback. The xmins should be cleared.
    standby_1.sql("ALTER SYSTEM SET hot_standby_feedback = off")
    standby_1.pg_ctl("reload")
    standby_2.sql("ALTER SYSTEM SET hot_standby_feedback = off")
    standby_2.pg_ctl("reload")
    replay_check()

    xmin, catalog_xmin = get_slot_xmins(
        primary, "standby_1", "xmin IS NULL AND catalog_xmin IS NULL"
    )
    assert xmin is None, "xmin of non-cascaded slot null with hs feedback reset"
    assert catalog_xmin is None, "catalog xmin of non-cascaded slot still null"

    xmin, catalog_xmin = get_slot_xmins(
        standby_1, "standby_2", "xmin IS NULL AND catalog_xmin IS NULL"
    )
    assert xmin is None, "xmin of cascaded slot null with hs feedback reset"
    assert catalog_xmin is None, "catalog xmin of cascaded slot still null"

    # Change primary_conninfo without restart: point standby_2 directly at the
    # primary instead of cascading through standby_1.
    standby_2.append_conf("primary_slot_name = ''")
    standby_2.enable_streaming(primary)
    standby_2.pg_ctl("reload")

    # The WAL receiver should have generated some IO statistics.
    assert (
        standby_1.sql(
            "SELECT sum(writes) > 0 FROM pg_stat_io "
            "WHERE backend_type = 'walreceiver' AND object = 'wal'"
        )
        is True
    ), "WAL receiver generates statistics for WAL writes"

    # Make sure standby_2 is no longer streaming from the cascade.
    standby_1.stop()
    newval = primary.sql(
        "INSERT INTO replayed(val) SELECT coalesce(max(val),0) + 1 AS newval "
        "FROM replayed RETURNING val"
    )
    primary.wait_for_catchup(standby_2)
    assert standby_2.sql(f"SELECT 1 FROM replayed WHERE val = {newval}") == 1, (
        f"standby_2 didn't replay primary value {newval}"
    )

    # Drop any existing slots on the primary for the follow-up tests.
    primary.sql("SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots")

    # Physical slot advancing and its durability. A new slot reserves WAL at
    # creation; advancing it recomputes the minimum LSN across all slots so the
    # previously-current segment becomes recyclable.
    primary.sql("SELECT pg_create_physical_replication_slot('phys_slot', true)")
    segment_removed = primary.sql("SELECT pg_walfile_name(pg_current_wal_lsn())")
    primary.advance_wal(1)
    current_lsn = primary.sql("SELECT pg_current_wal_lsn()")
    primary.sql(f"SELECT pg_replication_slot_advance('phys_slot', '{current_lsn}'::pg_lsn)")
    restart_lsn_pre = primary.sql(
        "SELECT restart_lsn FROM pg_replication_slots WHERE slot_name = 'phys_slot'"
    )
    # Slot advance should persist across clean restarts.
    primary.pg_ctl("restart")
    restart_lsn_post = primary.sql(
        "SELECT restart_lsn FROM pg_replication_slots WHERE slot_name = 'phys_slot'"
    )
    assert restart_lsn_pre == restart_lsn_post, (
        "physical slot advance persists across restarts"
    )
    # The previous segment should be recycled after the clean shutdown checkpoint.
    assert not (primary.datadir / "pg_wal" / segment_removed).exists(), (
        f"WAL segment {segment_removed} recycled after physical slot advancing"
    )

    # BASE_BACKUP cannot run in a session already running a backup. This needs a
    # replication connection with a database to mix a SQL and a replication
    # command.
    with primary.connect(replication="database", dbname="postgres") as c:
        c.sql("SELECT pg_backup_start('backup', true)")
        with pytest.raises(LibpqError, match="a backup is already in progress in this session"):
            c.sql("BASE_BACKUP")

    # BASE_BACKUP cancellation. Throttle the backup so there is room to cancel it
    # mid-stream; the follow-up pg_backup_stop() must then fail.
    bb = primary.background(replication="database", dbname="postgres")
    backup_future = bb.asql("BASE_BACKUP (CHECKPOINT 'fast', MAX_RATE 32)")
    try:
        # Cancel once the database files are streaming.
        primary.poll_query_until(
            "SELECT pg_cancel_backend(a.pid) FROM pg_stat_activity a, "
            "pg_stat_progress_basebackup b WHERE a.pid = b.pid AND "
            "a.query ~ 'BASE_BACKUP' AND b.phase = 'streaming database files'",
            True,
        )
        with pytest.raises(LibpqError):
            backup_future.result()
        with pytest.raises(LibpqError, match="backup is not in progress"):
            bb.sql("SELECT pg_backup_stop()")
    finally:
        bb.quit()
