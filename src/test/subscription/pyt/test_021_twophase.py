# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/021_twophase.pl.

Logical replication of two-phase commit: PREPARE TRANSACTION is replicated to
the subscriber as a prepared transaction and resolved when the publisher does
COMMIT/ROLLBACK PREPARED, including with a disabled subscriber (recovers after
raising max_prepared_transactions), across publisher/subscriber crash restarts,
with nested savepoints, an empty GID, and copy_data=false. Also exercises
toggling the two_phase subscription option (and simultaneously with failover)
and confirms the slot's two_phase flag tracks it.
"""

TWOPHASE_QUERY = (
    "SELECT count(1) = 0 FROM pg_subscription WHERE subtwophasestate NOT IN ('e')"
)


def test_twophase(create_pg):
    publisher = create_pg(
        "publisher", allows_streaming="logical", conf={"max_prepared_transactions": 10}
    )
    subscriber = create_pg("subscriber", conf={"max_prepared_transactions": 0})

    # pre-existing content on the publisher
    publisher.sql("CREATE TABLE tab_full (a int PRIMARY KEY)")
    publisher.sql_batch(
        "BEGIN",
        "INSERT INTO tab_full SELECT generate_series(1,10)",
        "PREPARE TRANSACTION 'some_initial_data'",
    )
    publisher.sql("COMMIT PREPARED 'some_initial_data'")

    subscriber.sql("CREATE TABLE tab_full (a int PRIMARY KEY)")

    connstr = publisher.connstr()
    publisher.sql("CREATE PUBLICATION tap_pub FOR TABLE tab_full")
    subscriber.sql(
        f"CREATE SUBSCRIPTION tap_sub CONNECTION '{connstr} application_name=tap_sub' "
        "PUBLICATION tap_pub WITH (two_phase = on)"
    )
    subscriber.wait_for_subscription_sync(publisher, "tap_sub")
    subscriber.poll_query_until(TWOPHASE_QUERY)

    # ===== 2PC replicated, then COMMIT PREPARED =============================
    log_location = subscriber.current_log_position()
    publisher.sql_batch(
        "BEGIN",
        "INSERT INTO tab_full VALUES (11)",
        "PREPARE TRANSACTION 'test_prepared_tab_full'",
    )

    # max_prepared_transactions = 0 on the subscriber makes the apply error.
    subscriber.wait_for_log(
        r"ERROR: ( [A-Z0-9]+:)? prepared transactions are disabled", log_location
    )

    # Raise max_prepared_transactions and resume replication.
    subscriber.append_conf(max_prepared_transactions=10)
    subscriber.pg_ctl("restart")
    publisher.wait_for_catchup("tap_sub")

    assert subscriber.sql("SELECT count(*) FROM pg_prepared_xacts") == 1, (
        "transaction is prepared on subscriber"
    )
    publisher.sql("COMMIT PREPARED 'test_prepared_tab_full'")
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT count(*) FROM tab_full where a = 11") == 1, (
        "Row inserted via 2PC has committed on subscriber"
    )
    assert subscriber.sql("SELECT count(*) FROM pg_prepared_xacts") == 0, (
        "transaction is committed on subscriber"
    )

    # ===== 2PC replicated, then ROLLBACK PREPARED ===========================
    publisher.sql_batch(
        "BEGIN",
        "INSERT INTO tab_full VALUES (12)",
        "PREPARE TRANSACTION 'test_prepared_tab_full'",
    )
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT count(*) FROM pg_prepared_xacts") == 1, (
        "transaction is prepared on subscriber"
    )
    publisher.sql("ROLLBACK PREPARED 'test_prepared_tab_full'")
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT count(*) FROM tab_full where a = 12") == 0, (
        "Row inserted via 2PC is not present on subscriber"
    )
    assert subscriber.sql("SELECT count(*) FROM pg_prepared_xacts") == 0, (
        "transaction is aborted on subscriber"
    )

    # ===== ROLLBACK PREPARED decoded after publisher+subscriber crash =======
    publisher.sql_batch(
        "BEGIN",
        "INSERT INTO tab_full VALUES (12)",
        "INSERT INTO tab_full VALUES (13)",
        "PREPARE TRANSACTION 'test_prepared_tab'",
    )
    subscriber.stop(mode="immediate")
    publisher.stop(mode="immediate")
    publisher.start()
    subscriber.start()
    publisher.sql("ROLLBACK PREPARED 'test_prepared_tab'")
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT count(*) FROM tab_full where a IN (12,13)") == 0, (
        "Rows rolled back are not on the subscriber"
    )

    # ===== COMMIT PREPARED decoded after publisher+subscriber crash =========
    publisher.sql_batch(
        "BEGIN",
        "INSERT INTO tab_full VALUES (12)",
        "INSERT INTO tab_full VALUES (13)",
        "PREPARE TRANSACTION 'test_prepared_tab'",
    )
    subscriber.stop(mode="immediate")
    publisher.stop(mode="immediate")
    publisher.start()
    subscriber.start()
    publisher.sql("COMMIT PREPARED 'test_prepared_tab'")
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT count(*) FROM tab_full where a IN (12,13)") == 2, (
        "Rows inserted via 2PC are visible on the subscriber"
    )

    # ===== COMMIT PREPARED decoded after subscriber-only crash ==============
    publisher.sql_batch(
        "BEGIN",
        "INSERT INTO tab_full VALUES (14)",
        "INSERT INTO tab_full VALUES (15)",
        "PREPARE TRANSACTION 'test_prepared_tab'",
    )
    subscriber.stop(mode="immediate")
    subscriber.start()
    publisher.sql("COMMIT PREPARED 'test_prepared_tab'")
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT count(*) FROM tab_full where a IN (14,15)") == 2, (
        "Rows inserted via 2PC are visible on the subscriber"
    )

    # ===== COMMIT PREPARED decoded after publisher-only crash ===============
    publisher.sql_batch(
        "BEGIN",
        "INSERT INTO tab_full VALUES (16)",
        "INSERT INTO tab_full VALUES (17)",
        "PREPARE TRANSACTION 'test_prepared_tab'",
    )
    publisher.stop(mode="immediate")
    publisher.start()
    publisher.sql("COMMIT PREPARED 'test_prepared_tab'")
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT count(*) FROM tab_full where a IN (16,17)") == 2, (
        "Rows inserted via 2PC are visible on the subscriber"
    )

    # ===== nested transaction with 2PC ======================================
    publisher.sql_batch(
        "BEGIN",
        "INSERT INTO tab_full VALUES (21)",
        "SAVEPOINT sp_inner",
        "INSERT INTO tab_full VALUES (22)",
        "ROLLBACK TO SAVEPOINT sp_inner",
        "PREPARE TRANSACTION 'outer'",
    )
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT count(*) FROM pg_prepared_xacts") == 1, (
        "transaction is prepared on subscriber"
    )
    publisher.sql("COMMIT PREPARED 'outer'")
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT count(*) FROM pg_prepared_xacts") == 0, (
        "transaction is ended on subscriber"
    )
    # 22 was rolled back, 21 committed.
    assert subscriber.sql("SELECT a FROM tab_full where a IN (21,22)") == 21, (
        "Rows committed are on the subscriber"
    )

    # ===== empty GID ========================================================
    publisher.sql_batch(
        "BEGIN",
        "INSERT INTO tab_full VALUES (51)",
        "PREPARE TRANSACTION ''",
    )
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT count(*) FROM pg_prepared_xacts") == 1, (
        "transaction is prepared on subscriber"
    )
    publisher.sql("ROLLBACK PREPARED ''")
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT count(*) FROM pg_prepared_xacts") == 0, (
        "transaction is aborted on subscriber"
    )

    # ===== copy_data=false and two_phase ====================================
    publisher.sql("CREATE TABLE tab_copy (a int PRIMARY KEY)")
    publisher.sql("INSERT INTO tab_copy SELECT generate_series(1,5)")
    subscriber.sql("CREATE TABLE tab_copy (a int PRIMARY KEY)")
    subscriber.sql("INSERT INTO tab_copy VALUES (88)")
    assert subscriber.sql("SELECT count(*) FROM tab_copy") == 1, (
        "initial data in subscriber table"
    )

    publisher.sql("CREATE PUBLICATION tap_pub_copy FOR TABLE tab_copy")
    subscriber.sql(
        f"CREATE SUBSCRIPTION tap_sub_copy "
        f"CONNECTION '{connstr} application_name=appname_copy' PUBLICATION tap_pub_copy "
        "WITH (two_phase=on, copy_data=false)"
    )
    subscriber.wait_for_subscription_sync(publisher, "appname_copy")
    subscriber.poll_query_until(TWOPHASE_QUERY)

    # copy_data=false: the initial data was NOT replicated.
    assert subscriber.sql("SELECT count(*) FROM tab_copy") == 1, (
        "initial data in subscriber table"
    )

    publisher.sql_batch(
        "BEGIN",
        "INSERT INTO tab_copy VALUES (99)",
        "PREPARE TRANSACTION 'mygid'",
    )
    publisher.wait_for_catchup("appname_copy")
    publisher.wait_for_catchup("tap_sub")
    # One prepared transaction per subscription.
    assert subscriber.sql("SELECT count(*) FROM pg_prepared_xacts") == 2, (
        "transaction is prepared on subscriber"
    )

    publisher.sql("COMMIT PREPARED 'mygid'")
    assert publisher.sql("SELECT count(*) FROM tab_copy") == 6, (
        "publisher inserted data"
    )
    publisher.wait_for_catchup("appname_copy")
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT count(*) FROM pg_prepared_xacts") == 0, (
        "should be no prepared transactions on subscriber"
    )
    assert subscriber.sql("SELECT count(*) FROM tab_copy") == 2, (
        "replicated data in subscriber table"
    )

    subscriber.sql("DROP SUBSCRIPTION tap_sub")

    # ===== ALTER SUBSCRIPTION two_phase -> false ============================
    assert (
        publisher.sql(
            "SELECT two_phase FROM pg_replication_slots WHERE slot_name = 'tap_sub_copy'"
        )
        is True
    ), "two-phase is enabled"

    subscriber.sql("ALTER SUBSCRIPTION tap_sub_copy DISABLE")
    subscriber.poll_query_until(
        "SELECT count(*) = 0 FROM pg_stat_activity "
        "WHERE backend_type = 'logical replication apply worker'"
    )
    subscriber.sql("ALTER SUBSCRIPTION tap_sub_copy SET (two_phase = false)")
    subscriber.sql("ALTER SUBSCRIPTION tap_sub_copy ENABLE")
    subscriber.wait_for_subscription_sync(publisher, "appname_copy")

    assert (
        subscriber.sql(
            "SELECT subtwophasestate FROM pg_subscription WHERE subname = 'tap_sub_copy'"
        )
        == "d"
    ), "two-phase subscription option should be disabled"
    assert (
        publisher.sql(
            "SELECT two_phase FROM pg_replication_slots WHERE slot_name = 'tap_sub_copy'"
        )
        is False
    ), "two-phase slot option should be disabled"

    # A prepare while two_phase is disabled is not replicated.
    publisher.sql_batch(
        "BEGIN",
        "INSERT INTO tab_copy VALUES (100)",
        "PREPARE TRANSACTION 'newgid'",
    )
    publisher.wait_for_catchup("appname_copy")
    assert subscriber.sql("SELECT count(*) FROM pg_prepared_xacts") == 0, (
        "should be no prepared transactions on subscriber"
    )

    # ===== set two_phase=true and failover=true simultaneously =============
    subscriber.sql("ALTER SUBSCRIPTION tap_sub_copy DISABLE")
    subscriber.poll_query_until(
        "SELECT count(*) = 0 FROM pg_stat_activity "
        "WHERE backend_type = 'logical replication apply worker'"
    )
    subscriber.sql(
        "ALTER SUBSCRIPTION tap_sub_copy SET (two_phase = true, failover = true)"
    )
    subscriber.sql("ALTER SUBSCRIPTION tap_sub_copy ENABLE")

    publisher.sql("COMMIT PREPARED 'newgid'")
    publisher.wait_for_catchup("appname_copy")
    assert subscriber.sql("SELECT count(*) FROM tab_copy") == 3, (
        "replicated data in subscriber table"
    )
    assert (
        subscriber.sql(
            "SELECT subtwophasestate FROM pg_subscription WHERE subname = 'tap_sub_copy'"
        )
        == "e"
    ), "two-phase should be enabled"

    subscriber.sql("DROP SUBSCRIPTION tap_sub_copy")
    publisher.sql("DROP PUBLICATION tap_pub_copy")

    # ===== cleanup ==========================================================
    assert subscriber.sql("SELECT count(*) FROM pg_subscription") == 0, (
        "check subscription was dropped on subscriber"
    )
    assert publisher.sql("SELECT count(*) FROM pg_replication_slots") == 0, (
        "check replication slot was dropped on publisher"
    )
    assert subscriber.sql("SELECT count(*) FROM pg_subscription_rel") == 0, (
        "check subscription relation status was dropped on subscriber"
    )
    assert subscriber.sql("SELECT count(*) FROM pg_replication_origin") == 0, (
        "check replication origin was dropped on subscriber"
    )
