# Copyright (c) 2023-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/035_standby_logical_decoding.pl.

Logical decoding on a standby: basic decoding, subscribing on the standby,
recovery-conflict invalidation of (active and inactive) logical slots across
several scenarios (vacuum full, row removal on a regular and a shared catalog,
no-conflict, on-access pruning, insufficient wal_level), persistence of
invalidation across restart, that invalidated slots don't retain WAL, that
DROP DATABASE drops its slots, and decoding across a standby promotion with a
cascading standby.
"""

import re
import subprocess
import threading
import time

import pytest

from libpq import LibpqError
from pypg import skip_unless_injection_points
from pypg._env import test_timeout_default

PRIMARY_SLOT = "primary_physical"
STANDBY_PHYSICAL_SLOT = "standby_physical"

EXPECTED_BEHAVES_OK = "\n".join(
    [
        "BEGIN",
        "table public.decoding_test: INSERT: x[integer]:1 y[text]:'1'",
        "table public.decoding_test: INSERT: x[integer]:2 y[text]:'2'",
        "table public.decoding_test: INSERT: x[integer]:3 y[text]:'3'",
        "table public.decoding_test: INSERT: x[integer]:4 y[text]:'4'",
        "COMMIT",
    ]
)

EXPECTED_PROMOTION = "\n".join(
    [
        "BEGIN",
        "table public.decoding_test: INSERT: x[integer]:1 y[text]:'1'",
        "table public.decoding_test: INSERT: x[integer]:2 y[text]:'2'",
        "table public.decoding_test: INSERT: x[integer]:3 y[text]:'3'",
        "table public.decoding_test: INSERT: x[integer]:4 y[text]:'4'",
        "COMMIT",
        "BEGIN",
        "table public.decoding_test: INSERT: x[integer]:5 y[text]:'5'",
        "table public.decoding_test: INSERT: x[integer]:6 y[text]:'6'",
        "table public.decoding_test: INSERT: x[integer]:7 y[text]:'7'",
        "COMMIT",
    ]
)


def test_standby_logical_decoding(create_pg, pg_bin):
    # Long test with many nodes and restarts: give each node a fresh full
    # timeout per poll rather than a shared per-test deadline.
    _create_pg = create_pg

    def create_pg(name, **kwargs):
        node = _create_pg(name, **kwargs)
        node.set_timeout(test_timeout_default)
        return node

    bindir = pg_bin.bindir

    primary = create_pg(
        "primary",
        allows_streaming=True,
        archiving=True,
        start=False,
        conf=[
            "wal_level = 'logical'",
            "max_replication_slots = 4",
            "max_wal_senders = 4",
            "autovacuum = off",
        ],
    )
    primary.start()
    skip_unless_injection_points(primary)

    primary.sql("CREATE DATABASE testdb")
    primary.sql(
        f"SELECT * FROM pg_create_physical_replication_slot('{PRIMARY_SLOT}')"
    )
    assert primary.sql(
        f"SELECT conflicting is null FROM pg_replication_slots WHERE slot_name = '{PRIMARY_SLOT}'"
    ) is True, "Physical slot reports conflicting as NULL"

    backup = primary.backup("b1")
    # VACUUM doesn't flush WAL; an insert into flush_wal outside a transaction
    # guarantees a flush so the standby can replay the preceding VACUUM.
    primary.sql("CREATE TABLE flush_wal()", dbname="testdb")

    standby = create_pg(
        "standby",
        from_backup=backup,
        streaming_primary=primary,
        restoring=primary,
        start=False,
        conf=[f"primary_slot_name = '{PRIMARY_SLOT}'", "max_replication_slots = 5"],
    )
    standby.start()
    primary.wait_for_catchup(standby)

    # --- helpers -------------------------------------------------------------

    def wait_for_xmins(node, slotname, expr):
        node.poll_query_until(
            f"SELECT {expr} FROM pg_catalog.pg_replication_slots "
            f"WHERE slot_name = '{slotname}'"
        )

    def create_logical_slots(node, prefix):
        node.create_logical_slot_on_standby(primary, prefix + "inactiveslot", "testdb")
        node.create_logical_slot_on_standby(primary, prefix + "activeslot", "testdb")

    def drop_logical_slots(prefix):
        for slot in (prefix + "inactiveslot", prefix + "activeslot"):
            try:
                standby.sql(f"SELECT pg_drop_replication_slot('{slot}')")
            except LibpqError:
                pass

    def make_slot_active(node, prefix, wait):
        active = prefix + "activeslot"
        proc = subprocess.Popen(
            [
                str(bindir / "pg_recvlogical"),
                "--dbname", node.connstr("testdb"),
                "--slot", active,
                "--option", "include-xids=0",
                "--option", "skip-empty-xacts=1",
                "--file", "-",
                "--no-loop", "--start",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if wait:
            node.poll_query_until(
                "SELECT EXISTS (SELECT 1 FROM pg_replication_slots "
                f"WHERE slot_name = '{active}' AND active_pid IS NOT NULL)",
                dbname="testdb",
            )
        return proc

    def check_recvlogical_stderr(proc, pattern):
        # The client should have terminated in response to the walsender error.
        _, err = proc.communicate(timeout=test_timeout_default())
        assert proc.returncode != 0, "pg_recvlogical exited non-zero"
        assert re.search(pattern, err), f"stderr {err!r} should match {pattern!r}"

    def change_hsf(hsf, invalidated):
        standby.adjust_conf("hot_standby_feedback", "on" if hsf else "off")
        standby.pg_ctl("reload")
        if hsf and invalidated:
            wait_for_xmins(primary, PRIMARY_SLOT, "xmin IS NOT NULL AND catalog_xmin IS NULL")
        elif hsf:
            wait_for_xmins(primary, PRIMARY_SLOT, "xmin IS NOT NULL AND catalog_xmin IS NOT NULL")
        else:
            wait_for_xmins(primary, PRIMARY_SLOT, "xmin IS NULL AND catalog_xmin IS NULL")

    def check_slots_conflict_reason(prefix, reason):
        for slot in (prefix + "activeslot", prefix + "inactiveslot"):
            assert standby.sql(
                f"select invalidation_reason from pg_replication_slots "
                f"where slot_name = '{slot}' and conflicting"
            ) == reason, f"{slot} reason for conflict is {reason}"

    def reactive(prev, prefix, hsf, invalidated):
        drop_logical_slots(prev)
        create_logical_slots(standby, prefix)
        change_hsf(hsf, invalidated)
        handle = make_slot_active(standby, prefix, True)
        # reset stats: easier to check confl_active_logicalslot afterwards
        standby.sql("select pg_stat_reset()", dbname="testdb")
        return handle

    def check_for_invalidation(prefix, log_start, name):
        for slot in (prefix + "inactiveslot", prefix + "activeslot"):
            standby.wait_for_log(
                f'invalidating obsolete replication slot "{slot}"', log_start
            )
        standby.poll_query_until(
            "select (confl_active_logicalslot = 1) from pg_stat_database_conflicts "
            "where datname = 'testdb'"
        )

    def wait_until_vacuum_can_remove(vac_option, sql, to_vac):
        # The injection point keeps the checkpointer/bgwriter from logging an
        # xl_running_xacts that could advance an active slot's catalog_xmin and
        # prevent the conflict.
        primary.sql(
            "SELECT injection_points_attach('skip-log-running-xacts', 'error')",
            dbname="testdb",
        )
        xid_horizon = primary.sql(
            "SELECT pg_snapshot_xmin(pg_current_snapshot())::text::bigint", dbname="testdb"
        )
        primary.sql(sql, dbname="testdb")
        primary.poll_query_until(
            "SELECT (pg_snapshot_xmin(pg_current_snapshot())::text::bigint "
            f"- {xid_horizon}) > 0",
            dbname="testdb",
        )
        primary.sql(f"VACUUM {vac_option} verbose {to_vac}", dbname="testdb")
        primary.sql("INSERT INTO flush_wal DEFAULT VALUES", dbname="testdb")
        primary.wait_for_catchup(standby)
        primary.sql(
            "SELECT injection_points_detach('skip-log-running-xacts')", dbname="testdb"
        )

    def pump_until(proc, pattern):
        # Read proc's stdout in a background thread until it matches pattern.
        chunks = []

        def reader():
            assert proc.stdout is not None
            for line in proc.stdout:
                chunks.append(line)

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        deadline = time.monotonic() + test_timeout_default()
        while time.monotonic() < deadline:
            if re.search(pattern, "".join(chunks), re.S):
                return "".join(chunks)
            time.sleep(0.1)
        raise TimeoutError(f"pg_recvlogical output never matched {pattern!r}")

    # --- the standby requires hot_standby for pre-existing logical slots -----
    standby.create_logical_slot_on_standby(primary, "restart_test", "testdb")
    standby.stop()
    standby.adjust_conf("hot_standby", "off")
    offset = standby.current_log_position()
    # The server is expected to fail during startup, so run pg_ctl directly
    # (the high-level start() would then fail reading the absent postmaster.pid)
    # and confirm the error via the log.
    try:
        standby.pg_ctl("start")
    except subprocess.CalledProcessError:
        pass
    standby.wait_for_log(
        r'logical replication slot ".*" exists on the standby, but "hot_standby" = "off"',
        offset,
    )
    standby.adjust_conf("hot_standby", "on")
    standby.start()
    standby.sql("SELECT pg_drop_replication_slot('restart_test')")

    # --- basic logical decoding on the standby -------------------------------
    create_logical_slots(standby, "behaves_ok_")
    primary.sql("CREATE TABLE decoding_test(x integer, y text)", dbname="testdb")
    primary.sql(
        "INSERT INTO decoding_test(x,y) SELECT s, s::text FROM generate_series(1,10) s",
        dbname="testdb",
    )
    primary.wait_for_catchup(standby)

    assert len(
        standby.sql(
            "SELECT pg_logical_slot_get_changes('behaves_ok_activeslot', NULL, NULL)",
            dbname="testdb",
        )
    ) == 14, "Decoding produced 14 rows (2 BEGIN/COMMIT and 10 rows)"

    primary.sql(
        "INSERT INTO decoding_test(x,y) SELECT s, s::text FROM generate_series(1,4) s",
        dbname="testdb",
    )
    primary.wait_for_catchup(standby)
    assert standby.sql(
        "SELECT data FROM pg_logical_slot_peek_changes('behaves_ok_activeslot', NULL, "
        "NULL, 'include-xids', '0', 'skip-empty-xacts', '1')",
        dbname="testdb",
    ) == EXPECTED_BEHAVES_OK.split("\n"), "got expected output from SQL decoding session"

    endpos = standby.sql(
        "SELECT lsn FROM pg_logical_slot_peek_changes('behaves_ok_activeslot', NULL, NULL) "
        "ORDER BY lsn DESC LIMIT 1",
        dbname="testdb",
    )
    primary.sql(
        "INSERT INTO decoding_test(x,y) SELECT s, s::text FROM generate_series(5,50) s",
        dbname="testdb",
    )
    primary.wait_for_catchup(standby)

    plugin_opts = {"include-xids": "0", "skip-empty-xacts": "1"}
    assert standby.pg_recvlogical_upto(
        "behaves_ok_activeslot", endpos, dbname="testdb", options=plugin_opts
    ) == EXPECTED_BEHAVES_OK, "got same expected output from pg_recvlogical decoding session"

    standby.poll_query_until(
        "SELECT EXISTS (SELECT 1 FROM pg_replication_slots "
        "WHERE slot_name = 'behaves_ok_activeslot' AND active_pid IS NULL)",
        dbname="testdb",
    )
    assert standby.pg_recvlogical_upto(
        "behaves_ok_activeslot", endpos, dbname="testdb", options=plugin_opts
    ) == "", "pg_recvlogical acknowledged changes"

    primary.sql("CREATE DATABASE otherdb")
    primary.wait_for_catchup(standby)
    with pytest.raises(
        LibpqError,
        match='replication slot "behaves_ok_activeslot" was not created in this database',
    ):
        standby.sql(
            "SELECT lsn FROM pg_logical_slot_peek_changes('behaves_ok_activeslot', NULL, NULL) "
            "ORDER BY lsn DESC LIMIT 1",
            dbname="otherdb",
        )

    # --- subscribe on the standby with a publication created on the primary --
    subscriber = create_pg("subscriber")
    primary.sql("CREATE TABLE tab_rep (a int primary key)")
    subscriber.sql("CREATE TABLE tab_rep (a int primary key)")
    primary.sql("CREATE PUBLICATION tap_pub for table tab_rep")
    primary.wait_for_catchup(standby)

    standby_connstr = standby.connstr() + " dbname=postgres"
    # CREATE SUBSCRIPTION creates a logical slot on the standby, which blocks
    # until the primary logs a standby snapshot; dispatch it and unblock it.
    sub_bg = subscriber.background()
    sub_future = sub_bg.asql(
        f"CREATE SUBSCRIPTION tap_sub CONNECTION '{standby_connstr}' "
        "PUBLICATION tap_pub WITH (copy_data = off)"
    )
    primary.log_standby_snapshot(standby, "tap_sub")
    sub_future.result()
    sub_bg.quit()
    subscriber.wait_for_subscription_sync(standby, "tap_sub")

    primary.sql("INSERT INTO tab_rep select generate_series(1,10)")
    primary.wait_for_catchup(standby)
    standby.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT count(*) FROM tab_rep") == 10, (
        "check replicated inserts after subscription on standby"
    )
    subscriber.sql("DROP SUBSCRIPTION tap_sub")
    subscriber.stop()

    primary.sql("CREATE EXTENSION injection_points", dbname="testdb")

    # --- Scenario 1: hot_standby_feedback off and VACUUM FULL ----------------
    handle = reactive("behaves_ok_", "vacuum_full_", False, True)
    primary.sql("INSERT INTO decoding_test(x,y) SELECT 100,'100'", dbname="testdb")
    standby.poll_query_until(
        "SELECT total_txns > 0 FROM pg_stat_replication_slots "
        "WHERE slot_name = 'vacuum_full_activeslot'",
        dbname="testdb",
    )
    wait_until_vacuum_can_remove(
        "full", "CREATE TABLE conflict_test(x integer, y text); DROP TABLE conflict_test;",
        "pg_class",
    )
    check_for_invalidation("vacuum_full_", 0, "with vacuum FULL on pg_class")
    check_slots_conflict_reason("vacuum_full_", "rows_removed")

    # Altering an invalidated slot errors.
    with standby.connect(replication="database") as repl:
        with pytest.raises(LibpqError) as exc:
            repl.sql("ALTER_REPLICATION_SLOT vacuum_full_inactiveslot (failover)")
        assert (
            'can no longer access replication slot "vacuum_full_inactiveslot"' in str(exc.value)
            and 'invalidated due to "rows_removed"' in (exc.value.detail or "")
        ), "invalidated slot cannot be altered"

    assert standby.sql(
        "SELECT total_txns > 0 FROM pg_stat_replication_slots "
        "WHERE slot_name = 'vacuum_full_activeslot'",
        dbname="testdb",
    ) is True, "replication slot stats not removed after invalidation"

    handle = make_slot_active(standby, "vacuum_full_", False)
    check_recvlogical_stderr(
        handle, 'can no longer access replication slot "vacuum_full_activeslot"'
    )

    # Copying an invalidated slot errors.
    with standby.connect(replication="database") as repl:
        with pytest.raises(
            LibpqError,
            match='cannot copy invalidated replication slot "vacuum_full_inactiveslot"',
        ):
            repl.sql(
                "select pg_copy_logical_replication_slot("
                "'vacuum_full_inactiveslot', 'vacuum_full_inactiveslot_copy')"
            )

    change_hsf(True, True)

    # Invalidation persists across a restart.
    standby.pg_ctl("restart")
    check_slots_conflict_reason("vacuum_full_", "rows_removed")

    # Invalidated logical slots don't retain WAL.
    restart_lsn = standby.sql(
        "SELECT restart_lsn FROM pg_replication_slots "
        "WHERE slot_name = 'vacuum_full_activeslot' AND conflicting"
    )
    walfile_name = primary.sql(f"SELECT pg_walfile_name('{restart_lsn}')")
    primary.advance_wal(1)
    primary.sql("checkpoint")
    primary.wait_for_catchup(standby)
    standby.sql("checkpoint")
    assert not (standby.datadir / "pg_wal" / walfile_name).exists(), (
        "invalidated logical slots do not lead to retaining WAL"
    )

    # --- Scenario 2: row removal with hot_standby_feedback off ---------------
    offset = standby.current_log_position()
    handle = reactive("vacuum_full_", "row_removal_", False, True)
    wait_until_vacuum_can_remove(
        "", "CREATE TABLE conflict_test(x integer, y text); DROP TABLE conflict_test;",
        "pg_class",
    )
    check_for_invalidation("row_removal_", offset, "with vacuum on pg_class")
    check_slots_conflict_reason("row_removal_", "rows_removed")
    handle = make_slot_active(standby, "row_removal_", False)
    check_recvlogical_stderr(
        handle, 'can no longer access replication slot "row_removal_activeslot"'
    )

    # --- Scenario 3: row removal on a shared catalog (pg_authid) -------------
    offset = standby.current_log_position()
    handle = reactive("row_removal_", "shared_row_removal_", False, True)
    wait_until_vacuum_can_remove(
        "", "CREATE ROLE create_trash; DROP ROLE create_trash;", "pg_authid"
    )
    check_for_invalidation("shared_row_removal_", offset, "with vacuum on pg_authid")
    check_slots_conflict_reason("shared_row_removal_", "rows_removed")
    handle = make_slot_active(standby, "shared_row_removal_", False)
    check_recvlogical_stderr(
        handle, 'can no longer access replication slot "shared_row_removal_activeslot"'
    )

    # --- Scenario 4: row removal on a non-catalog table: no conflict ---------
    offset = standby.current_log_position()
    handle = reactive("shared_row_removal_", "no_conflict_", False, True)
    wait_until_vacuum_can_remove(
        "",
        "CREATE TABLE conflict_test(x integer, y text); "
        "INSERT INTO conflict_test(x,y) SELECT s, s::text FROM generate_series(1,4) s; "
        "UPDATE conflict_test set x=1, y=1;",
        "conflict_test",
    )
    assert 'invalidating obsolete replication slot "no_conflict_inactiveslot"' not in (
        standby.log_since(offset)
    ), "inactiveslot not invalidated with vacuum on conflict_test"
    assert 'invalidating obsolete replication slot "no_conflict_activeslot"' not in (
        standby.log_since(offset)
    ), "activeslot not invalidated with vacuum on conflict_test"
    standby.poll_query_until(
        "select (confl_active_logicalslot = 0) from pg_stat_database_conflicts "
        "where datname = 'testdb'"
    )
    assert standby.sql(
        "select bool_or(conflicting) from (select conflicting from pg_replication_slots "
        "where slot_type = 'logical') s"
    ) is False, "Logical slots are reported as non conflicting"
    change_hsf(True, False)
    # The active no_conflict slot is still held by its pg_recvlogical; release it.
    handle.terminate()
    handle.wait()
    standby.pg_ctl("restart")

    # --- Scenario 5: on-access pruning ---------------------------------------
    offset = standby.current_log_position()
    handle = reactive("no_conflict_", "pruning_", False, False)
    primary.sql(
        "SELECT injection_points_attach('skip-log-running-xacts', 'error')",
        dbname="testdb",
    )
    primary.sql(
        "CREATE TABLE prun(id integer, s char(2000)) "
        "WITH (fillfactor = 75, user_catalog_table = true)",
        dbname="testdb",
    )
    primary.sql("INSERT INTO prun VALUES (1, 'A')", dbname="testdb")
    for s in ("B", "C", "D", "E"):
        primary.sql(f"UPDATE prun SET s = '{s}'", dbname="testdb")
    primary.wait_for_catchup(standby)
    primary.sql("SELECT injection_points_detach('skip-log-running-xacts')", dbname="testdb")
    check_for_invalidation("pruning_", offset, "with on-access pruning")
    check_slots_conflict_reason("pruning_", "rows_removed")
    handle = make_slot_active(standby, "pruning_", False)
    check_recvlogical_stderr(
        handle, 'can no longer access replication slot "pruning_activeslot"'
    )
    change_hsf(True, True)

    # --- Scenario 6: insufficient wal_level on the primary -------------------
    offset = standby.current_log_position()
    drop_logical_slots("pruning_")
    create_logical_slots(standby, "wal_level_")
    handle = make_slot_active(standby, "wal_level_", True)
    standby.sql("select pg_stat_reset()", dbname="testdb")
    primary.append_conf("wal_level = 'replica'")
    primary.pg_ctl("restart")
    primary.wait_for_catchup(standby)
    check_for_invalidation("wal_level_", offset, "due to wal_level")
    check_slots_conflict_reason("wal_level_", "wal_level_insufficient")
    handle = make_slot_active(standby, "wal_level_", False)
    check_recvlogical_stderr(
        handle,
        'logical decoding on standby requires "effective_wal_level" >= "logical" on the primary',
    )
    primary.append_conf("wal_level = 'logical'")
    primary.pg_ctl("restart")
    primary.wait_for_catchup(standby)
    handle = make_slot_active(standby, "wal_level_", False)
    check_recvlogical_stderr(
        handle, 'can no longer access replication slot "wal_level_activeslot"'
    )

    # --- DROP DATABASE drops its slots, including active ones ----------------
    drop_logical_slots("wal_level_")
    create_logical_slots(standby, "drop_db_")
    handle = make_slot_active(standby, "drop_db_", True)
    standby.create_logical_slot_on_standby(primary, "otherslot", "postgres")
    primary.sql("DROP DATABASE testdb")
    primary.wait_for_catchup(standby)
    assert standby.sql(
        "SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname = 'testdb')"
    ) is False, "database dropped on standby"
    for slot in ("drop_db_inactiveslot", "drop_db_activeslot"):
        assert standby.sql(
            f"SELECT slot_type FROM pg_replication_slots WHERE slot_name = '{slot}'"
        ) == [], f"{slot} on standby dropped"
    check_recvlogical_stderr(handle, "conflict with recovery")
    assert standby.sql(
        "SELECT slot_type FROM pg_replication_slots WHERE slot_name = 'otherslot'"
    ) == "logical", "otherslot on standby not dropped"
    standby.sql("SELECT pg_drop_replication_slot('otherslot')")

    # --- promotion and decoding behaviour afterwards -------------------------
    standby.pg_ctl("reload")
    primary.sql("CREATE DATABASE testdb")
    primary.sql("CREATE TABLE decoding_test(x integer, y text)", dbname="testdb")
    primary.wait_for_catchup(standby)

    # A physical slot on the standby for the cascading standby (created after
    # the WAL-retention test so it can't hold WAL back there).
    standby.sql(
        f"SELECT * FROM pg_create_physical_replication_slot('{STANDBY_PHYSICAL_SLOT}')",
        dbname="testdb",
    )

    cascade_backup = standby.backup("b1")
    cascading = create_pg(
        "cascading_standby",
        from_backup=cascade_backup,
        streaming_primary=standby,
        restoring=standby,
        start=False,
        conf=[f"primary_slot_name = '{STANDBY_PHYSICAL_SLOT}'", "hot_standby_feedback = on"],
    )
    cascading.start()

    create_logical_slots(standby, "promotion_")
    standby.wait_for_catchup(cascading, "replay", primary.lsn("flush"))
    create_logical_slots(cascading, "promotion_")

    handle = make_slot_active(standby, "promotion_", True)
    cascading_handle = make_slot_active(cascading, "promotion_", True)

    primary.sql(
        "INSERT INTO decoding_test(x,y) SELECT s, s::text FROM generate_series(1,4) s",
        dbname="testdb",
    )
    lsn = primary.lsn("flush")
    primary.wait_for_catchup(standby, "replay", lsn)
    standby.wait_for_catchup(cascading, "replay", lsn)

    standby.promote()
    standby.sql(
        "INSERT INTO decoding_test(x,y) SELECT s, s::text FROM generate_series(5,7) s",
        dbname="testdb",
    )
    standby.wait_for_catchup(cascading)

    assert standby.sql(
        "SELECT data FROM pg_logical_slot_peek_changes('promotion_inactiveslot', NULL, "
        "NULL, 'include-xids', '0', 'skip-empty-xacts', '1')",
        dbname="testdb",
    ) == EXPECTED_PROMOTION.split("\n"), (
        "got expected output from SQL decoding session on promoted standby"
    )

    assert pump_until(handle, r"COMMIT.*COMMIT").strip() == EXPECTED_PROMOTION, (
        "got same expected output from pg_recvlogical decoding session"
    )

    assert cascading.sql(
        "SELECT data FROM pg_logical_slot_peek_changes('promotion_inactiveslot', NULL, "
        "NULL, 'include-xids', '0', 'skip-empty-xacts', '1')",
        dbname="testdb",
    ) == EXPECTED_PROMOTION.split("\n"), (
        "got expected output from SQL decoding session on cascading standby"
    )

    assert pump_until(cascading_handle, r"COMMIT.*COMMIT").strip() == EXPECTED_PROMOTION, (
        "got same output from pg_recvlogical decoding session on cascading standby"
    )

    handle.terminate()
    handle.wait()
    cascading_handle.terminate()
    cascading_handle.wait()
