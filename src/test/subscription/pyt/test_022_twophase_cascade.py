# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/022_twophase_cascade.pl.

Cascading logical replication of two-phase commit across node_A -> node_B ->
node_C, for both non-streaming and streaming (over logical_decoding_work_mem)
subscriptions: a PREPARE on node_A appears as a prepared transaction on B and C
and is resolved on both when node_A does COMMIT/ROLLBACK PREPARED, including
nested savepoints rolled back before PREPARE.
"""

TWOPHASE_QUERY = (
    "SELECT count(1) = 0 FROM pg_subscription WHERE subtwophasestate NOT IN ('e')"
)


def test_twophase_cascade(create_pg):
    conf = {"max_prepared_transactions": 10, "logical_decoding_work_mem": "64kB"}
    node_A = create_pg("node_A", allows_streaming="logical", conf=conf)
    node_B = create_pg("node_B", allows_streaming="logical", conf=conf)
    node_C = create_pg("node_C", conf=conf)

    # pre-existing content on node_A
    node_A.sql("CREATE TABLE tab_full (a int PRIMARY KEY)")
    node_A.sql("INSERT INTO tab_full SELECT generate_series(1,10)")
    node_B.sql("CREATE TABLE tab_full (a int PRIMARY KEY)")
    node_C.sql("CREATE TABLE tab_full (a int PRIMARY KEY)")

    # test_tab for streaming tests; B and C have extra columns with defaults.
    node_A.sql("CREATE TABLE test_tab (a int primary key, b bytea)")
    node_A.sql("INSERT INTO test_tab VALUES (1, 'foo'), (2, 'bar')")
    node_B.sql(
        "CREATE TABLE test_tab (a int primary key, b bytea, "
        "c timestamptz DEFAULT now(), d bigint DEFAULT 999)"
    )
    node_C.sql(
        "CREATE TABLE test_tab (a int primary key, b bytea, "
        "c timestamptz DEFAULT now(), d bigint DEFAULT 999)"
    )

    # ===== 2PC NON-STREAMING setup ==========================================
    node_A_connstr = node_A.connstr()
    node_A.sql("CREATE PUBLICATION tap_pub_A FOR TABLE tab_full, test_tab")
    node_B.sql(
        f"CREATE SUBSCRIPTION tap_sub_B "
        f"CONNECTION '{node_A_connstr} application_name=tap_sub_B' PUBLICATION tap_pub_A "
        "WITH (two_phase = on, streaming = off)"
    )

    node_B_connstr = node_B.connstr()
    node_B.sql("CREATE PUBLICATION tap_pub_B FOR TABLE tab_full, test_tab")
    node_C.sql(
        f"CREATE SUBSCRIPTION tap_sub_C "
        f"CONNECTION '{node_B_connstr} application_name=tap_sub_C' PUBLICATION tap_pub_B "
        "WITH (two_phase = on, streaming = off)"
    )

    node_A.wait_for_catchup("tap_sub_B")
    node_B.wait_for_catchup("tap_sub_C")
    node_B.poll_query_until(TWOPHASE_QUERY)
    node_C.poll_query_until(TWOPHASE_QUERY)

    def both_subscribers_prepared(expected, msg):
        assert node_B.sql("SELECT count(*) FROM pg_prepared_xacts") == expected, (
            f"{msg} B"
        )
        assert node_C.sql("SELECT count(*) FROM pg_prepared_xacts") == expected, (
            f"{msg} C"
        )

    def cascade_catchup():
        node_A.wait_for_catchup("tap_sub_B")
        node_B.wait_for_catchup("tap_sub_C")

    # ===== 2PC replicated, then COMMIT PREPARED =============================
    node_A.sql_batch(
        "BEGIN",
        "INSERT INTO tab_full VALUES (11)",
        "PREPARE TRANSACTION 'test_prepared_tab_full'",
    )
    cascade_catchup()
    both_subscribers_prepared(1, "transaction is prepared on subscriber")

    node_A.sql("COMMIT PREPARED 'test_prepared_tab_full'")
    cascade_catchup()
    assert node_B.sql("SELECT count(*) FROM tab_full where a = 11") == 1, (
        "Row inserted via 2PC has committed on subscriber B"
    )
    assert node_C.sql("SELECT count(*) FROM tab_full where a = 11") == 1, (
        "Row inserted via 2PC has committed on subscriber C"
    )
    both_subscribers_prepared(0, "transaction is committed on subscriber")

    # ===== 2PC replicated, then ROLLBACK PREPARED ===========================
    node_A.sql_batch(
        "BEGIN",
        "INSERT INTO tab_full VALUES (12)",
        "PREPARE TRANSACTION 'test_prepared_tab_full'",
    )
    cascade_catchup()
    both_subscribers_prepared(1, "transaction is prepared on subscriber")

    node_A.sql("ROLLBACK PREPARED 'test_prepared_tab_full'")
    cascade_catchup()
    assert node_B.sql("SELECT count(*) FROM tab_full where a = 12") == 0, (
        "Row inserted via 2PC is not present on subscriber B"
    )
    assert node_C.sql("SELECT count(*) FROM tab_full where a = 12") == 0, (
        "Row inserted via 2PC is not present on subscriber C"
    )
    both_subscribers_prepared(0, "transaction is ended on subscriber")

    # ===== nested transaction with 2PC ======================================
    node_A.sql_batch(
        "BEGIN",
        "INSERT INTO tab_full VALUES (21)",
        "SAVEPOINT sp_inner",
        "INSERT INTO tab_full VALUES (22)",
        "ROLLBACK TO SAVEPOINT sp_inner",
        "PREPARE TRANSACTION 'outer'",
    )
    cascade_catchup()
    both_subscribers_prepared(1, "transaction is prepared on subscriber")

    node_A.sql("COMMIT PREPARED 'outer'")
    cascade_catchup()
    both_subscribers_prepared(0, "transaction is ended on subscriber")
    # 22 rolled back, 21 committed.
    assert node_B.sql("SELECT a FROM tab_full where a IN (21,22)") == 21, (
        "Rows committed are present on subscriber B"
    )
    assert node_C.sql("SELECT a FROM tab_full where a IN (21,22)") == 21, (
        "Rows committed are present on subscriber C"
    )

    # ===== 2PC + STREAMING setup ============================================
    oldpid_B = node_A.sql(
        "SELECT pid FROM pg_stat_replication "
        "WHERE application_name = 'tap_sub_B' AND state = 'streaming'"
    )
    oldpid_C = node_B.sql(
        "SELECT pid FROM pg_stat_replication "
        "WHERE application_name = 'tap_sub_C' AND state = 'streaming'"
    )
    node_B.sql("ALTER SUBSCRIPTION tap_sub_B SET (streaming = on)")
    node_C.sql("ALTER SUBSCRIPTION tap_sub_C SET (streaming = on)")
    node_A.poll_query_until(
        f"SELECT pid != {oldpid_B} FROM pg_stat_replication "
        "WHERE application_name = 'tap_sub_B' AND state = 'streaming'"
    )
    node_B.poll_query_until(
        f"SELECT pid != {oldpid_C} FROM pg_stat_replication "
        "WHERE application_name = 'tap_sub_C' AND state = 'streaming'"
    )

    # ===== streamed 2PC, then COMMIT PREPARED ===============================
    # Insert/update/delete enough rows to exceed the 64kB limit, then PREPARE.
    node_A.sql_batch(
        "BEGIN",
        "INSERT INTO test_tab SELECT i, sha256(i::text::bytea) FROM generate_series(3, 5000) s(i)",
        "UPDATE test_tab SET b = sha256(b) WHERE mod(a,2) = 0",
        "DELETE FROM test_tab WHERE mod(a,3) = 0",
        "PREPARE TRANSACTION 'test_prepared_tab'",
    )
    cascade_catchup()
    both_subscribers_prepared(1, "transaction is prepared on subscriber")

    node_A.sql("COMMIT PREPARED 'test_prepared_tab'")
    cascade_catchup()
    assert node_B.sql("SELECT count(*), count(c), count(d = 999) FROM test_tab") == (
        3334,
        3334,
        3334,
    ), (
        "Rows inserted by 2PC have committed on subscriber B, and extra columns have local defaults"
    )
    assert node_C.sql("SELECT count(*), count(c), count(d = 999) FROM test_tab") == (
        3334,
        3334,
        3334,
    ), (
        "Rows inserted by 2PC have committed on subscriber C, and extra columns have local defaults"
    )
    both_subscribers_prepared(0, "transaction is committed on subscriber")

    # ===== streamed 2PC with a nested ROLLBACK TO SAVEPOINT =================
    # Delete down to 2 rows (replicated), then prepare with a rolled-back savepoint.
    node_A.sql("DELETE FROM test_tab WHERE a > 2")
    node_A.sql_batch(
        "BEGIN",
        "INSERT INTO test_tab VALUES (9999, 'foobar')",
        "SAVEPOINT sp_inner",
        "INSERT INTO test_tab SELECT i, sha256(i::text::bytea) FROM generate_series(3, 5000) s(i)",
        "UPDATE test_tab SET b = sha256(b) WHERE mod(a,2) = 0",
        "DELETE FROM test_tab WHERE mod(a,3) = 0",
        "ROLLBACK TO SAVEPOINT sp_inner",
        "PREPARE TRANSACTION 'outer'",
    )
    cascade_catchup()
    both_subscribers_prepared(1, "transaction is prepared on subscriber")

    node_A.sql("COMMIT PREPARED 'outer'")
    cascade_catchup()
    both_subscribers_prepared(0, "transaction is ended on subscriber")

    # Everything after the savepoint rolled back; (9999, 'foobar') committed.
    assert node_B.sql("SELECT count(*) FROM test_tab where b = 'foobar'") == 1, (
        "Rows committed are present on subscriber B"
    )
    assert node_B.sql("SELECT count(*) FROM test_tab") == 3, (
        "Rows committed are present on subscriber B"
    )
    assert node_C.sql("SELECT count(*) FROM test_tab where b = 'foobar'") == 1, (
        "Rows committed are present on subscriber C"
    )
    assert node_C.sql("SELECT count(*) FROM test_tab") == 3, (
        "Rows committed are present on subscriber C"
    )

    # ===== cleanup ==========================================================
    node_C.sql("DROP SUBSCRIPTION tap_sub_C")
    assert node_C.sql("SELECT count(*) FROM pg_subscription") == 0, (
        "check subscription was dropped on subscriber node C"
    )
    assert node_C.sql("SELECT count(*) FROM pg_subscription_rel") == 0, (
        "check subscription relation status was dropped on subscriber node C"
    )
    assert node_C.sql("SELECT count(*) FROM pg_replication_origin") == 0, (
        "check replication origin was dropped on subscriber node C"
    )
    assert node_B.sql("SELECT count(*) FROM pg_replication_slots") == 0, (
        "check replication slot was dropped on publisher node B"
    )

    node_B.sql("DROP SUBSCRIPTION tap_sub_B")
    assert node_B.sql("SELECT count(*) FROM pg_subscription") == 0, (
        "check subscription was dropped on subscriber node B"
    )
    assert node_B.sql("SELECT count(*) FROM pg_subscription_rel") == 0, (
        "check subscription relation status was dropped on subscriber node B"
    )
    assert node_B.sql("SELECT count(*) FROM pg_replication_origin") == 0, (
        "check replication origin was dropped on subscriber node B"
    )
    assert node_A.sql("SELECT count(*) FROM pg_replication_slots") == 0, (
        "check replication slot was dropped on publisher node A"
    )
