# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/035_conflicts.pl.

Conflict detection in logical replication: multiple_unique_conflicts on
INSERT/UPDATE (including on a leaf partition), and — in a bidirectional A<->B
setup with retain_dead_tuples — delete_origin_differs and update_deleted
conflicts, the pg_conflict_detection slot and its xmin advancement, the rules
around enabling retain_dead_tuples, that dead tuples are retained until
concurrent remote transactions are flushed (including a publisher transaction
held at the commit-after-delay-checkpoint injection point), and that retention
stops/resumes with max_retention_duration.
"""

import re

from libpq import LibpqError, PostgresMessage, PostgresNotice, PostgresWarning

import pytest


def test_conflicts(create_pg):
    publisher = create_pg("publisher", allows_streaming="logical")
    subscriber = create_pg("subscriber", allows_streaming="logical")

    publisher.sql(
        "CREATE TABLE conf_tab (a int PRIMARY KEY, b int UNIQUE, c int UNIQUE)"
    )
    publisher.sql(
        "CREATE TABLE conf_tab_2 (a int PRIMARY KEY, b int UNIQUE, c int UNIQUE)"
    )
    subscriber.sql(
        "CREATE TABLE conf_tab (a int PRIMARY key, b int UNIQUE, c int UNIQUE)"
    )
    subscriber.sql_batch(
        "CREATE TABLE conf_tab_2 (a int PRIMARY KEY, b int, c int, unique(a,b)) PARTITION BY RANGE (a)",
        "CREATE TABLE conf_tab_2_p1 PARTITION OF conf_tab_2 FOR VALUES FROM (MINVALUE) TO (100)",
    )

    connstr = publisher.connstr()
    publisher.sql("CREATE PUBLICATION pub_tab FOR TABLE conf_tab, conf_tab_2")
    subscriber.sql(
        f"CREATE SUBSCRIPTION sub_tab "
        f"CONNECTION '{connstr} application_name=sub_tab' PUBLICATION pub_tab "
        f"WITH (conflict_log_destination=all)"
    )
    subscriber.wait_for_subscription_sync(publisher, "sub_tab")

    publisher.sql("INSERT INTO conf_tab VALUES (1,1,1)")
    subscriber.sql("INSERT INTO conf_tab VALUES (2,2,2), (3,3,3), (4,4,4)")

    # --- multiple_unique_conflicts on INSERT ---------------------------------
    offset = subscriber.current_log_position()
    publisher.sql("INSERT INTO conf_tab VALUES (2,3,4)")
    subscriber.wait_for_log(
        r'conflict detected on relation "public.conf_tab": conflict=multiple_unique_conflicts.*\n'
        r".*Could not apply remote change: remote row \(2, 3, 4\).*\n"
        r'.*Key already exists in unique index "conf_tab_pkey", modified in transaction .*: '
        r"key \(a\)=\(2\), local row \(2, 2, 2\).*\n"
        r'.*Key already exists in unique index "conf_tab_b_key", modified in transaction .*: '
        r"key \(b\)=\(3\), local row \(3, 3, 3\).*\n"
        r'.*Key already exists in unique index "conf_tab_c_key", modified in transaction .*: '
        r"key \(c\)=\(4\), local row \(4, 4, 4\)\.",
        offset,
    )
    subscriber.sql("TRUNCATE conf_tab")

    # --- multiple_unique_conflicts on UPDATE ---------------------------------
    publisher.sql("INSERT INTO conf_tab VALUES (5,5,5)")
    subscriber.sql("INSERT INTO conf_tab VALUES (6,6,6), (7,7,7), (8,8,8)")
    offset = subscriber.current_log_position()
    publisher.sql("UPDATE conf_tab set a=6, b=7, c=8 where a=5")
    subscriber.wait_for_log(
        r'conflict detected on relation "public.conf_tab": conflict=multiple_unique_conflicts.*\n'
        r".*Could not apply remote change: remote row \(6, 7, 8\), replica identity \(a\)=\(5\).*\n"
        r'.*Key already exists in unique index "conf_tab_pkey", modified in transaction .*: '
        r"key \(a\)=\(6\), local row \(6, 6, 6\).*\n"
        r'.*Key already exists in unique index "conf_tab_b_key", modified in transaction .*: '
        r"key \(b\)=\(7\), local row \(7, 7, 7\).*\n"
        r'.*Key already exists in unique index "conf_tab_c_key", modified in transaction .*: '
        r"key \(c\)=\(8\), local row \(8, 8, 8\)\.",
        offset,
    )
    subscriber.sql("TRUNCATE conf_tab")

    # --- multiple_unique_conflicts on INSERT into a leaf partition -----------
    subscriber.sql("INSERT INTO conf_tab_2 VALUES (55,2,3)")
    offset = subscriber.current_log_position()
    publisher.sql("INSERT INTO conf_tab_2 VALUES (55,2,3)")
    subscriber.wait_for_log(
        r'conflict detected on relation "public.conf_tab_2_p1": conflict=multiple_unique_conflicts.*\n'
        r".*Could not apply remote change: remote row \(55, 2, 3\).*\n"
        r'.*Key already exists in unique index "conf_tab_2_p1_pkey", modified in transaction .*: '
        r"key \(a\)=\(55\), local row \(55, 2, 3\).*\n"
        r'.*Key already exists in unique index "conf_tab_2_p1_a_b_key", modified in transaction .*: '
        r"key \(a, b\)=\(55, 2\), local row \(55, 2, 3\)\.",
        offset,
    )

    # ===== bidirectional replication A <-> B ================================
    node_A = publisher
    node_B = subscriber
    # track_commit_timestamp to detect origin-difference conflicts.
    node_A.append_conf(
        track_commit_timestamp=True, autovacuum=False, log_min_messages="debug2"
    )
    node_A.pg_ctl("restart")
    node_B.append_conf(track_commit_timestamp=True)
    node_B.pg_ctl("restart")

    node_A.sql("CREATE TABLE tab (a int PRIMARY KEY, b int)")
    node_B.sql("CREATE TABLE tab (a int PRIMARY KEY, b int)")

    subname_AB = "tap_sub_a_b"
    subname_BA = "tap_sub_b_a"

    node_A_connstr = node_A.connstr()
    node_A.sql("CREATE PUBLICATION tap_pub_A FOR TABLE tab")
    node_B.sql(
        f"CREATE SUBSCRIPTION {subname_BA} "
        f"CONNECTION '{node_A_connstr} application_name={subname_BA}' "
        "PUBLICATION tap_pub_A WITH (origin = none, retain_dead_tuples = true)"
    )

    node_B_connstr = node_B.connstr()
    node_B.sql("CREATE PUBLICATION tap_pub_B FOR TABLE tab")
    node_A.sql(
        f"CREATE SUBSCRIPTION {subname_AB} "
        f"CONNECTION '{node_B_connstr} application_name={subname_AB}' "
        "PUBLICATION tap_pub_B WITH (origin = none, copy_data = off)"
    )

    node_A.wait_for_subscription_sync(node_B, subname_AB)
    node_B.wait_for_subscription_sync(node_A, subname_BA)

    # The conflict-detection slot exists on Node B with a valid xmin.
    assert node_B.poll_query_until(
        "SELECT xmin IS NOT NULL from pg_replication_slots "
        "WHERE slot_name = 'pg_conflict_detection'"
    ), "the xmin value of slot 'pg_conflict_detection' is valid on Node B"

    # --- retain_dead_tuples can only be enabled for disabled subscriptions ---
    with pytest.raises(
        LibpqError,
        match=r'cannot set option "retain_dead_tuples" for enabled subscription',
    ):
        node_A.sql(f"ALTER SUBSCRIPTION {subname_AB} SET (retain_dead_tuples = true)")

    node_A.sql(f"ALTER SUBSCRIPTION {subname_AB} DISABLE")
    node_A.poll_query_until(
        "SELECT count(*) = 0 FROM pg_stat_activity "
        "WHERE backend_type = 'logical replication apply worker'"
    )
    with pytest.warns(
        PostgresNotice,
        match="deleted rows to detect conflicts would not be removed until the subscription is enabled",
    ):
        node_A.sql(f"ALTER SUBSCRIPTION {subname_AB} SET (retain_dead_tuples = true)")
    node_A.sql(f"ALTER SUBSCRIPTION {subname_AB} ENABLE")

    assert node_A.poll_query_until(
        "SELECT xmin IS NOT NULL from pg_replication_slots "
        "WHERE slot_name = 'pg_conflict_detection'"
    ), "the xmin value of slot 'pg_conflict_detection' is valid on Node A"

    # --- WARNING when changing origin to ANY with retain_dead_tuples ---------
    with pytest.warns(
        PostgresWarning,
        match='subscription "tap_sub_a_b" enabled retain_dead_tuples but might not '
        "reliably detect conflicts for changes from different origins",
    ):
        node_A.sql(f"ALTER SUBSCRIPTION {subname_AB} SET (origin = any)")
    node_A.sql(f"ALTER SUBSCRIPTION {subname_AB} SET (origin = none)")

    # --- dead tuples retained until remote txns flushed; update_deleted ------
    node_A.sql("INSERT INTO tab VALUES (1, 1), (2, 2)")
    node_A.wait_for_catchup(subname_BA)
    assert node_B.sql("SELECT * FROM tab") == [(1, 1), (2, 2)], (
        "check replicated insert on node B"
    )

    node_A.sql(f"ALTER SUBSCRIPTION {subname_AB} DISABLE")
    node_A.poll_query_until(
        "SELECT count(*) = 0 FROM pg_stat_activity "
        "WHERE backend_type = 'logical replication apply worker'"
    )

    log_location = node_B.current_log_position()
    node_B.sql("UPDATE tab SET b = 3 WHERE a = 1")
    node_A.sql("DELETE FROM tab WHERE a = 1")
    with pytest.warns(PostgresMessage, match="1 are dead but not yet removable"):
        node_A.sql("VACUUM (verbose) public.tab")

    node_A.wait_for_catchup(subname_BA)
    assert re.search(
        r'conflict detected on relation "public.tab": conflict=delete_origin_differs.*\n'
        r".*DETAIL:.* Deleting the row that was modified locally in transaction [0-9]+ at .*: "
        r"local row \(1, 3\), replica identity \(a\)=\(1\)\.",
        node_B.log_since(log_location),
    ), "delete target row was modified in tab"

    log_location = node_A.current_log_position()
    node_A.sql(f"ALTER SUBSCRIPTION {subname_AB} ENABLE")
    node_B.wait_for_catchup(subname_AB)
    assert re.search(
        r'conflict detected on relation "public.tab": conflict=update_deleted.*\n'
        r".*DETAIL:.* Could not find the row to be updated: remote row \(1, 3\), "
        r"replica identity \(a\)=\(1\).\n"
        r".*The row to be updated was deleted locally in transaction [0-9]+ at .*",
        node_A.log_since(log_location),
    ), "update target row was deleted in tab"

    next_xid = node_A.sql("SELECT txid_current() + 1")
    assert node_A.poll_query_until(
        f"SELECT xmin = {next_xid} from pg_replication_slots "
        "WHERE slot_name = 'pg_conflict_detection'"
    ), "the xmin value of slot 'pg_conflict_detection' is updated on Node A"

    # --- deleted tuple reachable via sequential scan (REPLICA IDENTITY FULL) -
    node_A.sql("ALTER TABLE tab REPLICA IDENTITY FULL")
    node_B.sql("ALTER TABLE tab REPLICA IDENTITY FULL")
    node_A.sql("ALTER TABLE tab DROP CONSTRAINT tab_pkey")
    node_A.sql(f"ALTER SUBSCRIPTION {subname_AB} DISABLE")
    node_A.poll_query_until(
        "SELECT count(*) = 0 FROM pg_stat_activity "
        "WHERE backend_type = 'logical replication apply worker'"
    )
    node_B.sql("UPDATE tab SET b = 4 WHERE a = 2")
    node_A.sql("DELETE FROM tab WHERE a = 2")

    log_location = node_A.current_log_position()
    node_A.sql(f"ALTER SUBSCRIPTION {subname_AB} ENABLE")
    node_B.wait_for_catchup(subname_AB)
    assert re.search(
        r'conflict detected on relation "public.tab": conflict=update_deleted.*\n'
        r".*DETAIL:.* Could not find the row to be updated: remote row \(2, 4\), "
        r"replica identity full \(2, 2\).*\n"
        r".*The row to be updated was deleted locally in transaction [0-9]+ at .*",
        node_A.log_since(log_location),
    ), "update target row was deleted in tab"

    # --- xmin advances when the subscription has no tables -------------------
    node_B.sql("ALTER PUBLICATION tap_pub_B DROP TABLE tab")
    node_A.sql(f"ALTER SUBSCRIPTION {subname_AB} REFRESH PUBLICATION")
    next_xid = node_A.sql("SELECT txid_current() + 1")
    assert node_A.poll_query_until(
        f"SELECT xmin = {next_xid} from pg_replication_slots "
        "WHERE slot_name = 'pg_conflict_detection'"
    ), "the xmin value of slot 'pg_conflict_detection' is updated on Node A"

    node_B.sql("ALTER PUBLICATION tap_pub_B ADD TABLE tab")
    node_A.sql(
        f"ALTER SUBSCRIPTION {subname_AB} REFRESH PUBLICATION WITH (copy_data = false)"
    )

    # --- DELAY_CHKPT_IN_COMMIT keeps deleted tuples (injection point) --------
    injection_points_supported = node_B.sql(
        "SELECT count(*) > 0 FROM pg_available_extensions WHERE name = 'injection_points'"
    )
    if injection_points_supported:
        node_B.append_conf(
            shared_preload_libraries="injection_points", max_prepared_transactions=1
        )
        node_B.pg_ctl("restart")

        # Test one-way replication only.
        node_B.sql(f"ALTER SUBSCRIPTION {subname_BA} DISABLE")
        node_B.poll_query_until(
            "SELECT count(*) = 0 FROM pg_stat_activity "
            "WHERE backend_type = 'logical replication apply worker'"
        )
        node_B.sql_batch("TRUNCATE tab", "INSERT INTO tab VALUES(1, 1)")
        node_B.wait_for_catchup(subname_AB)

        node_B.sql_batch(
            "CREATE EXTENSION injection_points",
            "SELECT injection_points_attach('commit-after-delay-checkpoint', 'wait')",
        )

        # Background session that pauses at the injection point during commit.
        pub_session = node_B.connect()
        pub_session.sql_batch(
            "BEGIN",
            "UPDATE tab SET b = 2 WHERE a = 1",
            "PREPARE TRANSACTION 'txn_with_later_commit_ts'",
        )
        commit_future = pub_session.background_sql(
            "COMMIT PREPARED 'txn_with_later_commit_ts'"
        )
        node_B.wait_for_event("client backend", "commit-after-delay-checkpoint")
        assert node_B.sql("SELECT * FROM tab WHERE a = 1") == (1, 1), (
            "publisher sees the old row"
        )

        # Delete on the subscriber; the row must be retained because of the
        # publisher transaction marked DELAY_CHKPT_IN_COMMIT.
        node_A.sql("DELETE FROM tab WHERE a = 1")
        sub_ts = node_A.sql("SELECT timestamp FROM pg_last_committed_xact()")

        # The apply worker repeatedly requests publisher status while waiting.
        log_location = node_A.current_log_position()
        node_A.wait_for_log(r"sending publisher status request message", log_location)
        log_location = node_A.current_log_position()
        node_A.wait_for_log(r"sending publisher status request message", log_location)

        with pytest.warns(PostgresMessage, match="1 are dead but not yet removable"):
            node_A.sql("VACUUM (verbose) public.tab")

        log_location = node_A.current_log_position()
        # Wake up the injection point so the prepared transaction commits.
        node_B.sql_batch(
            "SELECT injection_points_wakeup('commit-after-delay-checkpoint')",
            "SELECT injection_points_detach('commit-after-delay-checkpoint')",
        )
        commit_future.result()
        pub_session.close()

        assert node_B.sql("SELECT * FROM tab WHERE a = 1") == (1, 2), (
            "publisher sees the new row"
        )
        node_B.wait_for_catchup(subname_AB)
        assert re.search(
            r'conflict detected on relation "public.tab": conflict=update_deleted.*\n'
            r".*DETAIL:.* Could not find the row to be updated: remote row \(1, 2\), "
            r"replica identity full \(1, 1\).*\n"
            r".*The row to be updated was deleted locally in transaction [0-9]+ at .*",
            node_A.log_since(log_location),
        ), "update target row was deleted in tab"

        next_xid = node_A.sql("SELECT txid_current() + 1")
        assert node_A.poll_query_until(
            f"SELECT xmin = {next_xid} from pg_replication_slots "
            "WHERE slot_name = 'pg_conflict_detection'"
        ), "the xmin value of slot 'pg_conflict_detection' is updated on subscriber"

        pub_ts = node_B.sql("SELECT pg_xact_commit_timestamp(xmin) from tab where a=1")
        # The publisher UPDATE's commit timestamp is assigned after marking
        # DELAY_CHKPT_IN_COMMIT, so it is >= the subscriber DELETE's timestamp.
        assert (
            node_B.sql(f"SELECT '{pub_ts}'::timestamp >= '{sub_ts}'::timestamp") is True
        ), "pub UPDATE's timestamp is later than that of sub's DELETE"

        node_B.sql(f"ALTER SUBSCRIPTION {subname_BA} ENABLE")

    # --- retention stops past max_retention_duration -------------------------
    node_B.sql("SELECT * FROM pg_create_physical_replication_slot('blocker')")
    node_B.append_conf(synchronized_standby_slots="blocker")
    node_B.pg_ctl("reload")

    # Enabling failover activates synchronized_standby_slots.
    node_A.sql(f"ALTER SUBSCRIPTION {subname_AB} DISABLE")
    node_A.sql(f"ALTER SUBSCRIPTION {subname_AB} SET (failover = true)")
    node_A.sql(f"ALTER SUBSCRIPTION {subname_AB} ENABLE")

    node_B.sql("INSERT INTO tab VALUES (5, 5)")
    node_A.sql("SELECT txid_current() + 1")  # advance xid to trigger a cycle

    offset = node_A.current_log_position()
    node_A.sql(f"ALTER SUBSCRIPTION {subname_AB} SET (max_retention_duration = 1)")
    node_A.wait_for_log(
        r'logical replication worker for subscription "tap_sub_a_b" has stopped '
        r"retaining the information for detecting conflicts",
        offset,
    )
    assert node_A.poll_query_until(
        "SELECT xmin IS NULL from pg_replication_slots "
        "WHERE slot_name = 'pg_conflict_detection'"
    ), "the xmin value of slot 'pg_conflict_detection' is invalid on Node A"
    assert (
        node_A.sql(
            f"SELECT subretentionactive FROM pg_subscription WHERE subname='{subname_AB}'"
        )
        is False
    ), "retention is inactive"

    # --- retention resumes when max_retention_duration set to 0 --------------
    offset = node_A.current_log_position()
    node_A.sql(f"ALTER SUBSCRIPTION {subname_AB} SET (max_retention_duration = 0)")
    # Drop the blocker after setting 0 so resumption is immediate.
    node_B.sql("SELECT * FROM pg_drop_replication_slot('blocker')")
    node_B.adjust_conf(synchronized_standby_slots="")
    node_B.pg_ctl("reload")
    node_A.wait_for_log(
        r'logical replication worker for subscription "tap_sub_a_b" will resume '
        r"retaining the information for detecting conflicts\n"
        r".*DETAIL:.* Retention is re-enabled because max_retention_duration has been "
        r"set to unlimited.*",
        offset,
    )
    assert node_A.poll_query_until(
        "SELECT xmin IS NOT NULL from pg_replication_slots "
        "WHERE slot_name = 'pg_conflict_detection'"
    ), "the xmin value of slot 'pg_conflict_detection' is valid on Node A"
    assert (
        node_A.sql(
            f"SELECT subretentionactive FROM pg_subscription WHERE subname='{subname_AB}'"
        )
        is True
    ), "retention is active"

    # --- pg_conflict_detection slot dropped with the last subscription -------
    node_B.sql(f"DROP SUBSCRIPTION {subname_BA}")
    assert node_B.poll_query_until(
        "SELECT count(*) = 0 FROM pg_replication_slots "
        "WHERE slot_name = 'pg_conflict_detection'"
    ), "the slot 'pg_conflict_detection' has been dropped on Node B"

    node_A.sql(f"DROP SUBSCRIPTION {subname_AB}")
    assert node_A.poll_query_until(
        "SELECT count(*) = 0 FROM pg_replication_slots "
        "WHERE slot_name = 'pg_conflict_detection'"
    ), "the slot 'pg_conflict_detection' has been dropped on Node A"

    # --- a conflict log table is system-managed: ALTER TABLE SET TABLESPACE --
    # must be rejected directly on it.
    subid = subscriber.sql("SELECT oid FROM pg_subscription WHERE subname = 'sub_tab'")
    clt = f"pg_conflict.pg_conflict_log_{subid}"
    with pytest.raises(
        LibpqError, match=rf'cannot alter conflict log table "pg_conflict_log_{subid}"'
    ):
        subscriber.sql(f"ALTER TABLE {clt} SET TABLESPACE pg_default")

    # --- ALTER TABLE ALL IN TABLESPACE must skip conflict log tables, the ----
    # same way it skips catalog and TOAST tables, instead of failing. Use an
    # isolated database so the bulk move only touches the objects created
    # here.
    subscriber.sql("CREATE DATABASE clt_ts_test")
    subscriber.sql_oneshot(
        "CREATE SUBSCRIPTION sub_ts_test "
        "CONNECTION 'dbname=nonexistent' PUBLICATION pub "
        "WITH (connect=false, conflict_log_destination='table')",
        dbname="clt_ts_test",
    )

    # A plain user table that should be moved, alongside the CLT that must not
    # be.
    subscriber.sql_oneshot("CREATE TABLE user_tbl (i int)", dbname="clt_ts_test")

    # Create a tablespace backed by a directory inside the data dir.
    ts_dir = subscriber.datadir / "backup_space"
    ts_dir.mkdir()
    subscriber.sql(f"CREATE TABLESPACE backup_space LOCATION '{ts_dir}'")

    # The bulk move succeeds: the user table is relocated while the CLT is
    # skipped.
    subscriber.sql_oneshot(
        "ALTER TABLE ALL IN TABLESPACE pg_default SET TABLESPACE backup_space",
        dbname="clt_ts_test",
    )

    assert (
        subscriber.sql_oneshot(
            "SELECT reltablespace <> 0 FROM pg_class WHERE relname = 'user_tbl'",
            dbname="clt_ts_test",
        )
        is True
    ), "ALTER TABLE ALL IN TABLESPACE moves an ordinary user table"

    assert (
        subscriber.sql_oneshot(
            "SELECT count(*) FROM pg_class c JOIN pg_subscription s "
            "ON c.relname = 'pg_conflict_log_' || s.oid "
            "WHERE s.subname = 'sub_ts_test' AND c.reltablespace <> 0",
            dbname="clt_ts_test",
        )
        == 0
    ), "ALTER TABLE ALL IN TABLESPACE skips the conflict log table"

    # Cleanup. The subscription has no real publisher connection, so detach
    # its slot before dropping it.
    subscriber.sql_oneshot(
        "ALTER SUBSCRIPTION sub_ts_test DISABLE", dbname="clt_ts_test"
    )
    subscriber.sql_oneshot(
        "ALTER SUBSCRIPTION sub_ts_test SET (slot_name = NONE)", dbname="clt_ts_test"
    )
    subscriber.sql_oneshot("DROP SUBSCRIPTION sub_ts_test", dbname="clt_ts_test")
    subscriber.sql("DROP DATABASE clt_ts_test")
    subscriber.sql("DROP TABLESPACE backup_space")
