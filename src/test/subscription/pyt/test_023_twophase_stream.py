# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/023_twophase_stream.pl.

Logical replication of two-phase commit combined with streaming of large
in-progress transactions. The same battery of PREPARE/COMMIT PREPARED,
PREPARE/ROLLBACK PREPARED, crash-restart, and insert-after-PREPARE scenarios is
run for both streaming='on' and streaming='parallel'. Also covers serializing a
streamed prepared transaction to a file, and re-applying after a parallel apply
worker fails because max_prepared_transactions is too low.
"""


def test_twophase_stream(create_pg):
    publisher = create_pg(
        "publisher",
        allows_streaming="logical",
        conf={
            "max_prepared_transactions": 10,
            "debug_logical_replication_streaming": "immediate",
        },
    )
    subscriber = create_pg("subscriber", conf={"max_prepared_transactions": 10})

    def check_parallel_log(offset, is_parallel, kind):
        if is_parallel:
            subscriber.wait_for_log(
                rf"DEBUG: ( [A-Z0-9]+:)? finished processing the STREAM {kind} command",
                offset,
            )

    def test_streaming(appname, is_parallel):
        """Common steps for both streaming='on' and streaming='parallel'."""
        # --- streamed 2PC, then COMMIT PREPARED ------------------------------
        offset = subscriber.current_log_position()
        publisher.sql_batch(
            "BEGIN",
            "INSERT INTO test_tab SELECT i, sha256(i::text::bytea) FROM generate_series(3, 5) s(i)",
            "UPDATE test_tab SET b = sha256(b) WHERE mod(a,2) = 0",
            "DELETE FROM test_tab WHERE mod(a,3) = 0",
            "PREPARE TRANSACTION 'test_prepared_tab'",
        )
        publisher.wait_for_catchup(appname)
        check_parallel_log(offset, is_parallel, "PREPARE")
        assert subscriber.sql("SELECT count(*) FROM pg_prepared_xacts") == 1, (
            "transaction is prepared on subscriber"
        )

        publisher.sql("COMMIT PREPARED 'test_prepared_tab'")
        publisher.wait_for_catchup(appname)
        assert subscriber.sql(
            "SELECT count(*), count(c), count(d = 999) FROM test_tab"
        ) == (4, 4, 4), (
            "Rows inserted by 2PC have committed on subscriber, and extra columns contain local defaults"
        )
        assert subscriber.sql("SELECT count(*) FROM pg_prepared_xacts") == 0, (
            "transaction is committed on subscriber"
        )

        # --- streamed 2PC, then ROLLBACK PREPARED ----------------------------
        publisher.sql("DELETE FROM test_tab WHERE a > 2")
        offset = subscriber.current_log_position()
        publisher.sql_batch(
            "BEGIN",
            "INSERT INTO test_tab SELECT i, sha256(i::text::bytea) FROM generate_series(3, 5) s(i)",
            "UPDATE test_tab SET b = sha256(b) WHERE mod(a,2) = 0",
            "DELETE FROM test_tab WHERE mod(a,3) = 0",
            "PREPARE TRANSACTION 'test_prepared_tab'",
        )
        publisher.wait_for_catchup(appname)
        check_parallel_log(offset, is_parallel, "PREPARE")
        assert subscriber.sql("SELECT count(*) FROM pg_prepared_xacts") == 1, (
            "transaction is prepared on subscriber"
        )

        publisher.sql("ROLLBACK PREPARED 'test_prepared_tab'")
        publisher.wait_for_catchup(appname)
        assert subscriber.sql(
            "SELECT count(*), count(c), count(d = 999) FROM test_tab"
        ) == (2, 2, 2), (
            "Rows inserted by 2PC are rolled back, leaving only the original 2 rows"
        )
        assert subscriber.sql("SELECT count(*) FROM pg_prepared_xacts") == 0, (
            "transaction is aborted on subscriber"
        )

        # --- COMMIT PREPARED decoded after publisher+subscriber crash --------
        offset = subscriber.current_log_position()
        publisher.sql_batch(
            "BEGIN",
            "INSERT INTO test_tab SELECT i, sha256(i::text::bytea) FROM generate_series(3, 5) s(i)",
            "UPDATE test_tab SET b = sha256(b) WHERE mod(a,2) = 0",
            "DELETE FROM test_tab WHERE mod(a,3) = 0",
            "PREPARE TRANSACTION 'test_prepared_tab'",
        )
        subscriber.stop(mode="immediate")
        publisher.stop(mode="immediate")
        publisher.start()
        subscriber.start()
        # No parallel-log check here: the subscriber may have stopped after the
        # prepare but before logging.
        publisher.sql("COMMIT PREPARED 'test_prepared_tab'")
        publisher.wait_for_catchup(appname)
        assert subscriber.sql(
            "SELECT count(*), count(c), count(d = 999) FROM test_tab"
        ) == (4, 4, 4), (
            "Rows inserted by 2PC have committed on subscriber, and extra columns contain local defaults"
        )

        # --- INSERT after PREPARE but before ROLLBACK PREPARED ---------------
        publisher.sql("DELETE FROM test_tab WHERE a > 2")
        offset = subscriber.current_log_position()
        publisher.sql_batch(
            "BEGIN",
            "INSERT INTO test_tab SELECT i, sha256(i::text::bytea) FROM generate_series(3, 5) s(i)",
            "UPDATE test_tab SET b = sha256(b) WHERE mod(a,2) = 0",
            "DELETE FROM test_tab WHERE mod(a,3) = 0",
            "PREPARE TRANSACTION 'test_prepared_tab'",
        )
        publisher.wait_for_catchup(appname)
        check_parallel_log(offset, is_parallel, "PREPARE")
        assert subscriber.sql("SELECT count(*) FROM pg_prepared_xacts") == 1, (
            "transaction is prepared on subscriber"
        )

        # Separate primary key: the 2PC transaction still holds row locks.
        publisher.sql("INSERT INTO test_tab VALUES (99999, 'foobar')")
        publisher.sql("ROLLBACK PREPARED 'test_prepared_tab'")
        publisher.wait_for_catchup(appname)
        assert subscriber.sql(
            "SELECT count(*), count(c), count(d = 999) FROM test_tab"
        ) == (3, 3, 3), "check the outside insert was copied to subscriber"
        assert subscriber.sql("SELECT count(*) FROM pg_prepared_xacts") == 0, (
            "transaction is aborted on subscriber"
        )

        # --- INSERT after PREPARE but before COMMIT PREPARED -----------------
        publisher.sql("DELETE FROM test_tab WHERE a > 2")
        offset = subscriber.current_log_position()
        publisher.sql_batch(
            "BEGIN",
            "INSERT INTO test_tab SELECT i, sha256(i::text::bytea) FROM generate_series(3, 5) s(i)",
            "UPDATE test_tab SET b = sha256(b) WHERE mod(a,2) = 0",
            "DELETE FROM test_tab WHERE mod(a,3) = 0",
            "PREPARE TRANSACTION 'test_prepared_tab'",
        )
        publisher.wait_for_catchup(appname)
        check_parallel_log(offset, is_parallel, "PREPARE")
        assert subscriber.sql("SELECT count(*) FROM pg_prepared_xacts") == 1, (
            "transaction is prepared on subscriber"
        )

        publisher.sql("INSERT INTO test_tab VALUES (99999, 'foobar')")
        publisher.sql("COMMIT PREPARED 'test_prepared_tab'")
        publisher.wait_for_catchup(appname)
        assert subscriber.sql(
            "SELECT count(*), count(c), count(d = 999) FROM test_tab"
        ) == (5, 5, 5), (
            "Rows inserted by 2PC (as well as outside insert) have committed on subscriber, "
            "and extra columns contain local defaults"
        )
        assert subscriber.sql("SELECT count(*) FROM pg_prepared_xacts") == 0, (
            "transaction is committed on subscriber"
        )

        # Cleanup the test data.
        publisher.sql("DELETE FROM test_tab WHERE a > 2")
        publisher.wait_for_catchup(appname)

    # --- setup ---------------------------------------------------------------
    publisher.sql("CREATE TABLE test_tab (a int primary key, b bytea)")
    publisher.sql("INSERT INTO test_tab VALUES (1, 'foo'), (2, 'bar')")
    publisher.sql("CREATE TABLE test_tab_2 (a int)")
    subscriber.sql(
        "CREATE TABLE test_tab (a int primary key, b bytea, "
        "c timestamptz DEFAULT now(), d bigint DEFAULT 999)"
    )
    subscriber.sql("CREATE TABLE test_tab_2 (a int)")

    connstr = publisher.connstr()
    publisher.sql("CREATE PUBLICATION tap_pub FOR TABLE test_tab, test_tab_2")

    twophase_query = (
        "SELECT count(1) = 0 FROM pg_subscription WHERE subtwophasestate NOT IN ('e')"
    )

    # ===== streaming = 'on' =================================================
    subscriber.sql(
        f"CREATE SUBSCRIPTION tap_sub "
        f"CONNECTION '{connstr} application_name=tap_sub' PUBLICATION tap_pub "
        "WITH (streaming = on, two_phase = on)"
    )
    subscriber.wait_for_subscription_sync(publisher, "tap_sub")
    subscriber.poll_query_until(twophase_query)
    assert subscriber.sql(
        "SELECT count(*), count(c), count(d = 999) FROM test_tab"
    ) == (2, 2, 2), "check initial data was copied to subscriber"

    test_streaming("tap_sub", is_parallel=False)

    # ===== streaming = 'parallel' ===========================================
    oldpid = publisher.sql(
        "SELECT pid FROM pg_stat_replication "
        "WHERE application_name = 'tap_sub' AND state = 'streaming'"
    )
    subscriber.sql("ALTER SUBSCRIPTION tap_sub SET(streaming = parallel)")
    publisher.poll_query_until(
        f"SELECT pid != {oldpid} FROM pg_stat_replication "
        "WHERE application_name = 'tap_sub' AND state = 'streaming'"
    )
    # Need DEBUG logs to confirm the parallel apply worker applied the txn.
    subscriber.append_conf(log_min_messages="debug1")
    subscriber.pg_ctl("reload")
    subscriber.sql("SELECT 1")  # ensure the reload took effect

    test_streaming("tap_sub", is_parallel=True)

    # ===== serialize streamed changes to a file =============================
    subscriber.append_conf(debug_logical_replication_streaming="immediate")
    subscriber.append_conf(log_min_messages="warning")
    subscriber.pg_ctl("reload")
    subscriber.sql("SELECT 1")  # ensure the reload took effect

    offset = subscriber.current_log_position()
    publisher.sql_batch(
        "BEGIN",
        "INSERT INTO test_tab_2 values(1)",
        "PREPARE TRANSACTION 'xact'",
    )
    subscriber.wait_for_log(
        r"LOG: ( [A-Z0-9]+:)? logical replication apply worker will serialize the "
        r"remaining changes of remote transaction \d+ to a file",
        offset,
    )
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT count(*) FROM pg_prepared_xacts") == 1, (
        "transaction is prepared on subscriber"
    )

    publisher.sql("COMMIT PREPARED 'xact'")
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT count(*) FROM test_tab_2") == 1, (
        "transaction is committed on subscriber"
    )

    # ===== re-apply after parallel apply worker fails on max_prepared_xacts =
    subscriber.append_conf(max_prepared_transactions=0)
    subscriber.append_conf(debug_logical_replication_streaming="buffered")
    subscriber.pg_ctl("restart")

    publisher.sql_batch(
        "BEGIN",
        "INSERT INTO test_tab_2 values(2)",
        "PREPARE TRANSACTION 'xact'",
    )
    publisher.sql("COMMIT PREPARED 'xact'")

    offset = subscriber.current_log_position()
    subscriber.wait_for_log(
        r"ERROR: ( [A-Z0-9]+:)? prepared transactions are disabled", offset
    )
    # The worker-type keyword is checked rather than the exact message, which
    # varies by whether the leader detected the failure or was signalled.
    subscriber.wait_for_log(
        r"ERROR: .*logical replication parallel apply worker.*", offset
    )

    subscriber.append_conf(max_prepared_transactions=10)
    subscriber.pg_ctl("restart")
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT count(*) FROM test_tab_2") == 2, (
        "transaction is committed on subscriber after retrying"
    )

    # ===== cleanup ==========================================================
    subscriber.sql("DROP SUBSCRIPTION tap_sub")
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
