# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/049_wait_for_lsn.pl.

Exercises the WAIT FOR LSN command: the standby_replay/standby_write/
standby_flush modes on a standby and primary_flush on a primary, timeouts and
no_throw status reporting, subtransaction cleanup, parameter/mode validation,
many concurrent waiters per mode, promotion terminating standby waiters,
archive-only standbys (replay-position floor), fresh-shmem walreceiver startup,
off-by-one fencepost boundaries, and a timeline switch on a cascade standby.

The Perl test detects waiter completion by logging from a follow-up function;
here a blocked waiter is dispatched with ``background_sql()`` and its completion
observed by resolving the returned future, so the log helpers are unnecessary.
"""

import pytest
from libpq import LibpqError


def stop_walreceiver(node):
    """Clear primary_conninfo so the walreceiver stops, freezing its tracked
    positions. Returns the previous (quoted) value for resume_walreceiver."""
    saved = node.sql(
        "SELECT pg_catalog.quote_literal(setting) FROM pg_settings "
        "WHERE name = 'primary_conninfo'"
    )
    node.sql("ALTER SYSTEM SET primary_conninfo = ''")
    node.sql("SELECT pg_reload_conf()")

    node.poll_query_until("SELECT NOT EXISTS (SELECT * FROM pg_stat_wal_receiver)")
    return saved


def resume_walreceiver(node, saved):
    node.sql(f"ALTER SYSTEM SET primary_conninfo = {saved}")
    node.sql("SELECT pg_reload_conf()")

    node.poll_query_until("SELECT EXISTS (SELECT * FROM pg_stat_wal_receiver)")


def check_fencepost(node, mode, current_lsn, label):
    """Probe the wait predicate target <= currentLSN at the boundary: current
    and current-1 succeed, current+1 times out. Returns (lsn_minus, lsn_plus)."""
    lsn_minus = node.sql("SELECT ($1::pg_lsn - 1)::text", current_lsn)
    lsn_plus = node.sql("SELECT ($1::pg_lsn + 1)::text", current_lsn)
    for target, expected, timeout in (
        (current_lsn, "success", "5s"),
        (lsn_minus, "success", "5s"),
        (lsn_plus, "timeout", "500ms"),
    ):
        out = node.sql(
            f"WAIT FOR LSN '{target}' WITH (MODE '{mode}', timeout '{timeout}', no_throw)"
        )

        assert out == expected, f"{label}: target {target} expected {expected}"
    return lsn_minus, lsn_plus


def test_wait_for_lsn(create_pg):
    """A streaming standby: the wait modes, timeouts and status reporting,
    subtransaction cleanup, parameter and mode validation, many concurrent
    waiters per mode, and promotion terminating the waiters.

    These run against one primary/standby pair in sequence, and have to: the
    visibility check builds on the rows the previous section inserted, and the
    promotion at the end leaves the standby no longer a standby."""
    primary = create_pg("primary", allows_streaming=True)
    primary.sql("CREATE TABLE wait_test AS SELECT generate_series(1,10) AS a")
    backup = primary.backup("my_backup")

    # Streaming standby with a 1 second apply delay.
    standby = create_pg(
        "standby",
        from_backup=backup,
        streaming_primary=primary,
        start=False,
        conf={"recovery_min_apply_delay": "1s"},
    )
    standby.start()

    def insert_lsn(values):
        primary.sql(f"INSERT INTO wait_test VALUES ({values})")
        return primary.sql("SELECT pg_current_wal_insert_lsn()")

    # 1. WAIT FOR works: wait for the primary's insert LSN to be replayed.
    lsn1 = insert_lsn("generate_series(11, 20)")
    standby.sql(f"WAIT FOR LSN '{lsn1}' WITH (timeout '1d')")

    assert standby.sql("SELECT pg_lsn_cmp(pg_last_wal_replay_lsn(), $1)", lsn1) >= 0, (
        "standby reached the same LSN as primary after WAIT FOR"
    )

    # 2. New data is visible after WAIT FOR.
    lsn2 = insert_lsn("generate_series(21, 30)")
    standby.sql(f"WAIT FOR LSN '{lsn2}'")

    assert standby.sql("SELECT count(*) FROM wait_test") == 30, (
        "standby reached the same LSN as primary"
    )

    # 3. standby_write, standby_flush, and primary_flush modes.
    lsn_write = insert_lsn("generate_series(31, 40)")
    standby.sql(f"WAIT FOR LSN '{lsn_write}' WITH (MODE 'standby_write', timeout '1d')")

    assert (
        standby.sql(
            "SELECT pg_lsn_cmp((SELECT written_lsn FROM pg_stat_wal_receiver), $1)",
            lsn_write,
        )
        >= 0
    ), "standby wrote WAL up to target LSN with MODE 'standby_write'"

    lsn_flush = insert_lsn("generate_series(41, 50)")
    standby.sql(f"WAIT FOR LSN '{lsn_flush}' WITH (MODE 'standby_flush', timeout '1d')")

    assert (
        standby.sql("SELECT pg_lsn_cmp(pg_last_wal_receive_lsn(), $1)", lsn_flush) >= 0
    ), "standby flushed WAL up to target LSN with MODE 'standby_flush'"

    lsn_primary_flush = insert_lsn("generate_series(51, 60)")
    primary.sql(
        f"WAIT FOR LSN '{lsn_primary_flush}' WITH (MODE 'primary_flush', timeout '1d')"
    )

    assert (
        primary.sql(
            "SELECT pg_lsn_cmp(pg_current_wal_flush_lsn(), $1)",
            lsn_primary_flush,
        )
        >= 0
    ), "primary flushed WAL up to target LSN with MODE 'primary_flush'"

    # 4. Waiting for an unreachable LSN times out.
    lsn3 = primary.sql("SELECT pg_current_wal_insert_lsn() + 10000000000")
    standby.sql(f"WAIT FOR LSN '{lsn2}' WITH (timeout '10ms')")
    with pytest.raises(LibpqError, match="timed out while waiting for target LSN"):
        standby.sql(f"WAIT FOR LSN '{lsn3}' WITH (timeout '1000ms')")
    assert (
        standby.sql(f"WAIT FOR LSN '{lsn2}' WITH (timeout '0.1s', no_throw)")
        == "success"
    ), "WAIT FOR returns correct status after successful waiting"

    assert (
        standby.sql(f"WAIT FOR LSN '{lsn3}' WITH (timeout '10ms', no_throw)")
        == "timeout"
    ), "WAIT FOR returns correct status after timeout"

    # 4a. Aborting a subtransaction during WAIT FOR cleans up the shared
    # wait-state, so a later WAIT FOR in the same backend can register again.
    subxact_lsn = primary.sql("SELECT pg_current_wal_insert_lsn() + 10000000000")
    sub = primary.connect()
    sub.sql("SET application_name = 'wait_for_lsn_subxact_cleanup'")
    sub.sql("BEGIN")
    sub.sql("SAVEPOINT wait_cleanup")
    blocked = sub.background_sql(
        f"WAIT FOR LSN '{subxact_lsn}' WITH (MODE 'primary_flush')"
    )

    primary.poll_query_until(
        "SELECT count(*) = 1 FROM pg_stat_activity "
        "WHERE application_name = 'wait_for_lsn_subxact_cleanup' "
        "AND wait_event = 'WaitForWalFlush'"
    )

    assert primary.sql(
        "SELECT pg_cancel_backend(pid) FROM pg_stat_activity "
        "WHERE application_name = 'wait_for_lsn_subxact_cleanup' "
        "AND wait_event = 'WaitForWalFlush'"
    ), "canceled WAIT FOR LSN in subtransaction"
    with pytest.raises(LibpqError, match="canceling statement due to user request"):
        blocked.result()
    sub.sql("ROLLBACK TO wait_cleanup")

    assert (
        sub.sql(
            f"WAIT FOR LSN '{subxact_lsn}' WITH (MODE 'primary_flush', timeout '10ms', no_throw)"
        )
        == "timeout"
    ), "second WAIT FOR LSN timed out after savepoint rollback"
    sub.sql("COMMIT")
    sub.close()

    # 5. Mode validation and context restrictions.
    with pytest.raises(LibpqError, match="recovery is not in progress"):
        primary.sql(f"WAIT FOR LSN '{lsn3}' WITH (MODE 'standby_flush')")
    with pytest.raises(LibpqError, match="recovery is in progress"):
        standby.sql(f"WAIT FOR LSN '{lsn3}' WITH (MODE 'primary_flush')")
    with standby.connect() as c:
        c.sql("BEGIN ISOLATION LEVEL REPEATABLE READ")
        c.sql("SELECT 1")
        with pytest.raises(
            LibpqError,
            match="WAIT FOR must be called without an active or registered snapshot",
        ):
            c.sql(f"WAIT FOR LSN '{lsn3}'")

    primary.sql_batch(
        """
        CREATE FUNCTION pg_wal_replay_wait_wrap(target_lsn pg_lsn) RETURNS void AS $$
          BEGIN
            EXECUTE format('WAIT FOR LSN %L;', target_lsn);
          END
        $$ LANGUAGE plpgsql;
        """,
        """
        CREATE PROCEDURE pg_wal_replay_wait_proc(target_lsn pg_lsn) AS $$
          BEGIN
            EXECUTE format('WAIT FOR LSN %L;', target_lsn);
          END
        $$ LANGUAGE plpgsql;
        """,
    )

    primary.wait_for_catchup(standby)
    top_level = "WAIT FOR can only be executed as a top-level statement"
    with pytest.raises(LibpqError, match=top_level):
        standby.sql("SELECT pg_wal_replay_wait_wrap($1)", lsn3)
    with pytest.raises(LibpqError, match=top_level):
        standby.sql("CALL pg_wal_replay_wait_proc($1)", lsn3)
    with pytest.raises(LibpqError, match=top_level):
        standby.sql(f"DO $$ BEGIN EXECUTE format('WAIT FOR LSN %L;', '{lsn3}'); END $$")

    # 6. Parameter-validation error cases.
    test_lsn = primary.sql("SELECT pg_current_wal_insert_lsn()")
    cases = [
        (
            f"WAIT FOR LSN '{test_lsn}' WITH (timeout '-1000ms')",
            "timeout cannot be negative",
        ),
        (
            f"WAIT FOR LSN '{test_lsn}' WITH (unknown_param 'value')",
            'option "unknown_param" not recognized',
        ),
        (
            f"WAIT FOR LSN '{test_lsn}' WITH (timeout '1000', timeout '2000')",
            "conflicting or redundant options",
        ),
        (
            f"WAIT FOR LSN '{test_lsn}' WITH (no_throw, no_throw)",
            "conflicting or redundant options",
        ),
        (f"WAIT FOR LSN '{test_lsn}' (timeout '100ms')", "syntax error"),
        ("WAIT FOR TIMEOUT 1000", "syntax error"),
        ("WAIT FOR LSN 'invalid_lsn'", "invalid input syntax for type pg_lsn"),
        (
            f"WAIT FOR LSN '{test_lsn}' WITH (timeout 'invalid')",
            "invalid timeout value",
        ),
        (
            f"WAIT FOR LSN '{test_lsn}' WITH (invalid_option 'value')",
            'option "invalid_option" not recognized',
        ),
        (
            f"WAIT FOR LSN '{test_lsn}' WITH (MODE 'invalid')",
            'unrecognized value for WAIT option "mode": "invalid"',
        ),
        (
            f"WAIT FOR LSN '{test_lsn}' WITH (MODE 'standby_replay', MODE 'standby_write')",
            "conflicting or redundant options",
        ),
    ]
    for query, msg in cases:
        with pytest.raises(LibpqError, match=msg):
            standby.sql(query)

    assert (
        standby.sql(f"WAIT FOR LSN '{lsn2}' WITH (timeout '0.1s', no_throw)")
        == "success"
    ), "WAIT FOR WITH clause syntax works correctly"

    assert (
        standby.sql(f"WAIT FOR LSN '{lsn3}' WITH (timeout 100, no_throw)") == "timeout"
    ), "WAIT FOR WITH clause returns correct timeout status"

    # 7a. Multiple standby_replay waiters unblock as replay advances, and each
    # one sees the rows its target LSN covers.
    #
    # Each waiter gets a held connection rather than a oneshot one so the row
    # count can be read back in the session that did the waiting, which is what
    # the Perl log_count() function checked (it only logged when the count had
    # reached the expected value, and the test waited for that log line).
    standby.sql("SELECT pg_wal_replay_pause()")
    replay_waiters = []
    for i in range(5):
        lsn = insert_lsn(str(i))

        # Read the count from the primary rather than hardcoding it: the
        # sections above this one decide how many rows exist by now.
        expected_count = primary.sql("SELECT count(*) FROM wait_test")
        session = standby.connect()
        replay_waiters.append(
            (session, session.background_sql(f"WAIT FOR LSN '{lsn}'"), expected_count)
        )
    standby.sql("SELECT pg_wal_replay_resume()")
    for i, (session, fut, expected_count) in enumerate(replay_waiters):
        fut.result()

        assert session.sql("SELECT count(*) FROM wait_test") >= expected_count, (
            f"standby_replay waiter {i} sees the rows up to its target LSN"
        )
        session.close()

    # 7b/7c. Multiple standby_write / standby_flush waiters that block until the
    # walreceiver is resumed.
    def multi_mode_waiters(mode, base, wait_event):
        saved = stop_walreceiver(standby)
        lsns = [insert_lsn(str(base + i)) for i in range(5)]
        waiters = [
            standby.background_sql_oneshot(
                f"WAIT FOR LSN '{lsns[i]}' WITH (MODE '{mode}', timeout '1d')"
            )
            for i in range(5)
        ]

        standby.poll_query_until(
            "SELECT count(*) = 5 FROM pg_stat_activity WHERE wait_event = $1",
            wait_event,
        )
        resume_walreceiver(standby, saved)
        for fut in waiters:
            fut.result()
        return lsns

    write_lsns = multi_mode_waiters("standby_write", 100, "WaitForWalWrite")

    assert (
        standby.sql(
            "SELECT pg_lsn_cmp((SELECT written_lsn FROM pg_stat_wal_receiver), $1)",
            write_lsns[4],
        )
        >= 0
    ), "multiple standby_write waiters: standby wrote WAL up to target LSN"

    flush_lsns = multi_mode_waiters("standby_flush", 200, "WaitForWalFlush")

    assert (
        standby.sql("SELECT pg_lsn_cmp(pg_last_wal_receive_lsn(), $1)", flush_lsns[4])
        >= 0
    ), "multiple standby_flush waiters: standby flushed WAL up to target LSN"

    # 7d. Mixed-mode waiters with both walreceiver stopped and replay paused.
    saved = stop_walreceiver(standby)
    standby.sql("SELECT pg_wal_replay_pause()")
    mixed_target = insert_lsn("generate_series(301, 310)")
    modes = ("standby_replay", "standby_write", "standby_flush")
    mixed = [
        standby.background_sql_oneshot(
            f"WAIT FOR LSN '{mixed_target}' WITH (MODE '{modes[i % 3]}', timeout '1d')"
        )
        for i in range(6)
    ]

    standby.poll_query_until(
        "SELECT count(*) = 6 FROM pg_stat_activity WHERE wait_event LIKE 'WaitForWal%'"
    )
    standby.sql("SELECT pg_wal_replay_resume()")

    standby.poll_query_until("SELECT NOT pg_is_wal_replay_paused()")
    resume_walreceiver(standby, saved)
    for fut in mixed:
        fut.result()
    assert standby.sql(
        "SELECT pg_lsn_cmp((SELECT written_lsn FROM pg_stat_wal_receiver), $1) >= 0 "
        "AND pg_lsn_cmp(pg_last_wal_receive_lsn(), $1) >= 0 "
        "AND pg_lsn_cmp(pg_last_wal_replay_lsn(), $1) >= 0",
        mixed_target,
    ), "mixed mode waiters: all modes completed and reached target LSN"

    # 7e. Multiple primary_flush waiters on the primary (WAL already flushed).
    pf_lsns = [insert_lsn(str(400 + i)) for i in range(5)]
    pf_waiters = [
        primary.background_sql_oneshot(
            f"WAIT FOR LSN '{pf_lsns[i]}' WITH (MODE 'primary_flush', timeout '1d')"
        )
        for i in range(5)
    ]
    for fut in pf_waiters:
        fut.result()
    assert (
        primary.sql("SELECT pg_lsn_cmp(pg_current_wal_flush_lsn(), $1)", pf_lsns[4])
        >= 0
    ), "multiple primary_flush waiters: primary flushed WAL up to target LSN"

    # 8. Standby promotion terminates all standby wait modes.
    lsn4 = primary.sql("SELECT pg_current_wal_insert_lsn() + 10000000000")
    lsn5 = primary.sql("SELECT pg_current_wal_insert_lsn()")
    wait_modes = ("standby_replay", "standby_write", "standby_flush")
    promote_waiters = [
        standby.background_sql_oneshot(f"WAIT FOR LSN '{lsn4}' WITH (MODE '{mode}')")
        for mode in wait_modes
    ]

    # Ensure all three waiters have registered before promoting.
    standby.poll_query_until(
        "SELECT count(*) = 3 FROM pg_stat_activity WHERE wait_event LIKE 'WaitForWal%'"
    )
    primary.sql("SELECT pg_switch_wal()")

    primary.wait_for_catchup(standby)
    log_offset = standby.current_log_position()
    standby.promote()

    # Each waiting mode logs a distinct interruption message on promotion.
    for word in ("was written", "was flushed", "was replayed"):
        standby.wait_for_log(f"Recovery ended before target LSN.*{word}", log_offset)
    # The client sessions are abandoned (their errors are irrelevant).
    for fut in promote_waiters:
        try:
            fut.result()
        except Exception:
            pass
    # Waiting for an already-replayed LSN exits immediately even after promotion.
    standby.sql(f"WAIT FOR LSN '{lsn5}'")

    assert (
        standby.sql(f"WAIT FOR LSN '{lsn4}' WITH (timeout '10ms', no_throw)")
        == "not in recovery"
    ), "WAIT FOR returns correct status after standby promotion"
    standby.stop()
    primary.stop()


def test_wait_for_lsn_archive_only_standby(create_pg):
    """standby_write and standby_flush on a standby with no walreceiver.

    Exercises the replay-position floor in GetCurrentLSNForWaitType(): with WAL
    arriving only by archive recovery, the walreceiver-tracked positions stay at
    their zero-initialised values."""
    # 9. Archive-only standby: standby_write/standby_flush use the replay floor.
    arc_primary = create_pg("arc_primary", allows_streaming=True, archiving=True)
    arc_primary.sql("CREATE TABLE arc_test AS SELECT generate_series(1,10) AS a")
    arc_backup = arc_primary.backup("arc_backup")
    arc_primary.sql("INSERT INTO arc_test VALUES (generate_series(11, 20))")
    arc_target = arc_primary.sql("SELECT pg_current_wal_insert_lsn()")

    def archive_switch(node):
        segment = node.sql("SELECT pg_walfile_name(pg_current_wal_lsn())")
        node.sql("SELECT pg_switch_wal()")

        node.poll_query_until(
            "SELECT last_archived_wal >= $1 FROM pg_stat_archiver", segment
        )

    archive_switch(arc_primary)

    arc_standby = create_pg(
        "arc_standby", from_backup=arc_backup, restoring=arc_primary
    )

    arc_standby.poll_query_until(
        "SELECT pg_wal_lsn_diff(pg_last_wal_replay_lsn(), $1) >= 0", arc_target
    )

    assert arc_standby.sql("SELECT count(*) FROM pg_stat_wal_receiver") == 0, (
        "arc_standby has no walreceiver"
    )

    # 9a. Getter fallback: already-replayed LSN succeeds immediately.
    for mode in ("standby_write", "standby_flush"):
        assert (
            arc_standby.sql(
                f"WAIT FOR LSN '{arc_target}' WITH (MODE '{mode}', timeout '3s', no_throw)"
            )
            == "success"
        ), f"{mode} succeeds on archive-only standby (getter fallback)"

    # 9b. Sleeping waiters are woken by replay catching up.
    arc_standby.sql("SELECT pg_wal_replay_pause()")
    arc_primary.sql("INSERT INTO arc_test VALUES (generate_series(21, 30))")
    arc_target2 = arc_primary.sql("SELECT pg_current_wal_insert_lsn()")
    archive_switch(arc_primary)
    arc_wf = arc_standby.background_sql_oneshot(
        f"WAIT FOR LSN '{arc_target2}' WITH (MODE 'standby_write', timeout '1d', no_throw)"
    )
    arc_ff = arc_standby.background_sql_oneshot(
        f"WAIT FOR LSN '{arc_target2}' WITH (MODE 'standby_flush', timeout '1d', no_throw)"
    )

    arc_standby.poll_query_until(
        "SELECT count(*) = 2 FROM pg_stat_activity WHERE wait_event LIKE 'WaitForWal%'"
    )
    arc_standby.sql("SELECT pg_wal_replay_resume()")

    assert arc_wf.result() == "success", "standby_write waiter woken by replay"
    assert arc_ff.result() == "success", "standby_flush waiter woken by replay"

    arc_standby.stop()
    arc_primary.stop()


def test_wait_for_lsn_fresh_walreceiver(create_pg):
    """Walreceiver startup with fresh shared memory, and the fencepost.

    RequestXLogStreaming() seeds writtenUpto/flushedUpto from the
    segment-aligned receiveStart, which the replay floor then has to cover; the
    boundary checks probe the wait predicate at target == current +/- 1."""
    # 10/11. Fresh-shmem walreceiver startup and off-by-one fencepost checks.
    rcv_primary = create_pg(
        "rcv_primary", allows_streaming=True, conf={"autovacuum": False}
    )
    rcv_primary.sql("CREATE TABLE rcv_test AS SELECT generate_series(1,10) AS a")
    rcv_backup = rcv_primary.backup("rcv_backup")
    rcv_standby = create_pg(
        "rcv_standby", from_backup=rcv_backup, streaming_primary=rcv_primary
    )

    rcv_primary.sql("INSERT INTO rcv_test VALUES (generate_series(11, 100))")
    rcv_primary.sql("SELECT pg_switch_wal()")
    rcv_primary.sql("INSERT INTO rcv_test VALUES (generate_series(101, 110))")

    rcv_primary.wait_for_catchup(rcv_standby)

    # Restart the standby with the primary down so the walreceiver can't update
    # writtenUpto/flushedUpto past the initial value.
    rcv_standby.stop()
    rcv_primary.stop()
    rcv_standby.start()

    rcv_standby.poll_query_until("SELECT pg_last_wal_receive_lsn() IS NOT NULL")
    rcv_standby.sql("SELECT pg_wal_replay_pause()")

    rcv_standby.poll_query_until("SELECT pg_get_wal_replay_pause_state() = 'paused'")

    rcv_receive = rcv_standby.sql("SELECT pg_last_wal_receive_lsn()")
    rcv_replay = rcv_standby.sql("SELECT pg_last_wal_replay_lsn()")

    assert rcv_standby.sql(
        "SELECT pg_wal_lsn_diff($1, $2) > 0", rcv_replay, rcv_receive
    ), "replay sits ahead of initial walreceiver flush position"

    assert (
        rcv_standby.sql(
            "SELECT mod(pg_wal_lsn_diff($1, '0/0'::pg_lsn), setting::numeric)::int "
            "FROM pg_settings WHERE name = 'wal_segment_size'",
            rcv_receive,
        )
        == 0
    ), "initial walreceiver flush position is segment-aligned"

    for mode in ("standby_write", "standby_flush"):
        assert (
            rcv_standby.sql(
                f"WAIT FOR LSN '{rcv_replay}' WITH (MODE '{mode}', timeout '5s', no_throw)"
            )
            == "success"
        ), f"{mode} succeeds for already-replayed LSN after standby restart"

    # Restore the primary and generate fresh WAL so the walreceiver advances.
    rcv_standby.sql("SELECT pg_wal_replay_resume()")
    rcv_primary.start()
    rcv_primary.sql("INSERT INTO rcv_test VALUES (generate_series(111, 120))")

    rcv_primary.wait_for_catchup(rcv_standby)

    # 11. Fencepost boundary checks with replay and walreceiver frozen.
    saved = stop_walreceiver(rcv_standby)
    rcv_standby.sql("SELECT pg_wal_replay_pause()")

    rcv_standby.poll_query_until("SELECT pg_get_wal_replay_pause_state() = 'paused'")

    replay_lsn = rcv_standby.sql("SELECT pg_last_wal_replay_lsn()")
    _, replay_lsn_plus = check_fencepost(
        rcv_standby, "standby_replay", replay_lsn, "standby_replay"
    )

    flush_lsn = rcv_standby.sql("SELECT pg_last_wal_receive_lsn()")

    assert rcv_standby.sql(
        "SELECT pg_wal_lsn_diff($1, $2) >= 0", flush_lsn, replay_lsn
    ), "standby_flush boundary is not masked by replay floor"
    check_fencepost(rcv_standby, "standby_flush", flush_lsn, "standby_flush")

    # 11c. A sleeping waiter at current + 1 wakes once replay advances past it.
    rcv_primary.sql("INSERT INTO rcv_test VALUES (generate_series(200, 210))")
    boundary_fut = rcv_standby.background_sql_oneshot(
        f"WAIT FOR LSN '{replay_lsn_plus}' WITH (MODE 'standby_replay', timeout '1d', no_throw)"
    )

    rcv_standby.poll_query_until(
        "SELECT count(*) > 0 FROM pg_stat_activity WHERE wait_event = 'WaitForWalReplay'"
    )
    rcv_standby.sql("SELECT pg_wal_replay_resume()")
    resume_walreceiver(rcv_standby, saved)

    assert boundary_fut.result() == "success", (
        "standby_replay: waiter at current + 1 wakes when replay advances"
    )
    rcv_standby.stop()
    rcv_primary.stop()


def test_wait_for_lsn_cascade_timeline_switch(create_pg):
    """A waiter on a cascade standby survives its upstream's promotion.

    The cascade walreceiver has to reconnect on the new timeline and keep
    replaying across the boundary for the waiter to be satisfied."""
    # 12. Timeline switch on a cascade standby: a WAIT FOR waiter must survive
    # its upstream's promotion.
    tl_primary = create_pg(
        "tl_primary", allows_streaming=True, conf={"autovacuum": False}
    )
    tl_primary.sql("CREATE TABLE tl_test AS SELECT generate_series(1, 10) AS a")
    tl_backup = tl_primary.backup("tl_backup")
    tl_standby1 = create_pg(
        "tl_standby1", from_backup=tl_backup, streaming_primary=tl_primary
    )
    tl_backup2 = tl_standby1.backup("tl_backup2")
    tl_standby2 = create_pg(
        "tl_standby2", from_backup=tl_backup2, streaming_primary=tl_standby1
    )

    tl_primary.sql("INSERT INTO tl_test VALUES (generate_series(11, 20))")
    lsn = tl_primary.lsn("flush")

    tl_primary.wait_for_catchup(tl_standby1, "replay", lsn)
    tl_standby1.wait_for_catchup(tl_standby2, "replay", lsn)

    # Target past the current insert LSN, so reaching it requires WAL produced
    # on the new timeline.
    tl_target = tl_primary.sql("SELECT (pg_current_wal_insert_lsn() + 65536)::text")
    tl_standby2.sql("SELECT pg_wal_replay_pause()")

    tl_standby2.poll_query_until("SELECT pg_get_wal_replay_pause_state() = 'paused'")

    tl_fut = tl_standby2.background_sql_oneshot(
        f"WAIT FOR LSN '{tl_target}' WITH (MODE 'standby_replay', timeout '1d', no_throw)"
    )

    tl_standby2.poll_query_until(
        "SELECT count(*) > 0 FROM pg_stat_activity WHERE wait_event = 'WaitForWalReplay'"
    )

    tl_standby1.promote()
    tl_standby1.sql("INSERT INTO tl_test VALUES (generate_series(21, 1020))")
    tl_standby1.sql("SELECT pg_switch_wal()")
    tl_standby2.sql("SELECT pg_wal_replay_resume()")

    tl_standby2.poll_query_until("SELECT received_tli > 1 FROM pg_stat_wal_receiver")

    assert tl_fut.result() == "success", (
        "WAIT FOR LSN survives upstream promotion and timeline switch on cascade standby"
    )
