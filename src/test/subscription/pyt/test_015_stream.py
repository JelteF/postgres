# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/015_stream.pl.

Streaming of large in-progress transactions (exceeding
logical_decoding_work_mem) for streaming='on' and streaming='parallel',
including binary mode and locally-changed extra columns on the subscriber. Then
verifies that deadlocks are detected between the leader and a parallel apply
worker, and between two parallel apply workers (resolved by dropping the
conflicting unique index), and that changes serialized to a file are replayed.
"""


def test_stream(create_pg):
    publisher = create_pg(
        "publisher",
        allows_streaming="logical",
        conf={"logical_decoding_work_mem": "64kB"},
    )
    subscriber = create_pg("subscriber")

    def check_parallel_log(offset, is_parallel, kind):
        if is_parallel:
            subscriber.wait_for_log(
                rf"DEBUG: ( [A-Z0-9]+:)? finished processing the STREAM {kind} command",
                offset,
            )

    def test_streaming(appname, is_parallel):
        """Common steps for streaming='on' and streaming='parallel'."""
        # Interleave two transactions, each exceeding the 64kB limit.
        h = publisher.connect()
        offset = subscriber.current_log_position()
        h.sql_batch(
            "BEGIN",
            "INSERT INTO test_tab SELECT i, sha256(i::text::bytea) FROM generate_series(3, 5000) s(i)",
            "UPDATE test_tab SET b = sha256(b) WHERE mod(a,2) = 0",
            "DELETE FROM test_tab WHERE mod(a,3) = 0",
        )
        publisher.sql_batch(
            "BEGIN",
            "INSERT INTO test_tab SELECT i, sha256(i::text::bytea) FROM generate_series(5001, 9999) s(i)",
            "DELETE FROM test_tab WHERE a > 5000",
            "COMMIT",
        )
        h.sql("COMMIT")
        h.close()
        publisher.wait_for_catchup(appname)
        check_parallel_log(offset, is_parallel, "COMMIT")
        assert subscriber.sql(
            "SELECT count(*), count(c), count(d = 999) FROM test_tab"
        ) == (
            3334,
            3334,
            3334,
        ), "check extra columns contain local defaults"

        # Streaming in binary mode.
        subscriber.sql("ALTER SUBSCRIPTION tap_sub SET (binary = on)")
        offset = subscriber.current_log_position()
        publisher.sql_batch(
            "BEGIN",
            "INSERT INTO test_tab SELECT i, sha256(i::text::bytea) FROM generate_series(5001, 10000) s(i)",
            "UPDATE test_tab SET b = sha256(b) WHERE mod(a,2) = 0",
            "DELETE FROM test_tab WHERE mod(a,3) = 0",
            "COMMIT",
        )
        publisher.wait_for_catchup(appname)
        check_parallel_log(offset, is_parallel, "COMMIT")
        assert subscriber.sql(
            "SELECT count(*), count(c), count(d = 999) FROM test_tab"
        ) == (
            6667,
            6667,
            6667,
        ), "check extra columns contain local defaults"

        # Locally change the extra columns and confirm a non-streaming txn after
        # a streaming one preserves them.
        subscriber.sql(
            "UPDATE test_tab SET c = 'epoch'::timestamptz + 987654321 * interval '1s'"
        )
        offset = subscriber.current_log_position()
        publisher.sql("UPDATE test_tab SET b = sha256(a::text::bytea)")
        publisher.wait_for_catchup(appname)
        check_parallel_log(offset, is_parallel, "COMMIT")
        assert subscriber.sql(
            "SELECT count(*), count(extract(epoch from c) = 987654321), count(d = 999) FROM test_tab"
        ) == (6667, 6667, 6667), "check extra columns contain locally changed data"

        # Cleanup the test data.
        publisher.sql("DELETE FROM test_tab WHERE (a > 2)")
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
    subscriber.sql("CREATE UNIQUE INDEX idx_tab on test_tab_2(a)")

    connstr = publisher.connstr()
    publisher.sql("CREATE PUBLICATION tap_pub FOR TABLE test_tab, test_tab_2")

    # ===== streaming = 'on' =================================================
    subscriber.sql(
        f"CREATE SUBSCRIPTION tap_sub "
        f"CONNECTION '{connstr} application_name=tap_sub' PUBLICATION tap_pub "
        "WITH (streaming = on)"
    )
    subscriber.wait_for_subscription_sync(publisher, "tap_sub")
    assert subscriber.sql(
        "SELECT count(*), count(c), count(d = 999) FROM test_tab"
    ) == (2, 2, 2), "check initial data was copied to subscriber"

    test_streaming("tap_sub", is_parallel=False)

    # ===== streaming = 'parallel' ===========================================
    oldpid = publisher.sql(
        "SELECT pid FROM pg_stat_replication "
        "WHERE application_name = 'tap_sub' AND state = 'streaming'"
    )
    subscriber.sql("ALTER SUBSCRIPTION tap_sub SET(streaming = parallel, binary = off)")
    publisher.poll_query_until(
        f"SELECT pid != {oldpid} FROM pg_stat_replication "
        "WHERE application_name = 'tap_sub' AND state = 'streaming'"
    )
    # Need DEBUG logs to confirm the parallel apply worker applied the txn.
    subscriber.append_conf(log_min_messages="debug1")
    subscriber.pg_ctl("reload")
    subscriber.sql("SELECT 1")  # ensure the reload took effect

    test_streaming("tap_sub", is_parallel=True)

    # ===== deadlock between leader and parallel apply worker ================
    subscriber.append_conf(deadlock_timeout="10ms")
    subscriber.pg_ctl("reload")
    subscriber.sql("SELECT 1")  # ensure the reload took effect

    h = publisher.connect()
    offset = subscriber.current_log_position()
    h.sql_batch(
        "BEGIN", "INSERT INTO test_tab_2 SELECT i FROM generate_series(1, 5000) s(i)"
    )
    # Ensure the parallel apply worker runs the insert before the leader.
    subscriber.wait_for_log(
        r"DEBUG: ( [A-Z0-9]+:)? applied [0-9]+ changes in the streaming chunk", offset
    )
    publisher.sql("INSERT INTO test_tab_2 values(1)")
    h.sql("COMMIT")
    h.close()
    subscriber.wait_for_log(r"ERROR: ( [A-Z0-9]+:)? deadlock detected", offset)

    # Drop the unique index so the two transactions can complete.
    subscriber.sql("DROP INDEX idx_tab")
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT count(*) FROM test_tab_2") == 5001, (
        "data replicated to subscriber after dropping index"
    )

    publisher.sql("TRUNCATE TABLE test_tab_2")
    publisher.wait_for_catchup("tap_sub")
    subscriber.sql("CREATE UNIQUE INDEX idx_tab on test_tab_2(a)")

    # ===== deadlock between two parallel apply workers ======================
    h = publisher.connect()
    offset = subscriber.current_log_position()
    h.sql_batch(
        "BEGIN", "INSERT INTO test_tab_2 SELECT i FROM generate_series(1, 5000) s(i)"
    )
    # Ensure the first parallel apply worker runs the insert before the second.
    subscriber.wait_for_log(
        r"DEBUG: ( [A-Z0-9]+:)? applied [0-9]+ changes in the streaming chunk", offset
    )
    publisher.sql("INSERT INTO test_tab_2 SELECT i FROM generate_series(1, 5000) s(i)")
    h.sql("COMMIT")
    h.close()
    subscriber.wait_for_log(r"ERROR: ( [A-Z0-9]+:)? deadlock detected", offset)

    subscriber.sql("DROP INDEX idx_tab")
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT count(*) FROM test_tab_2") == 10000, (
        "data replicated to subscriber after dropping index"
    )

    # ===== serialize changes to a file ======================================
    subscriber.append_conf(debug_logical_replication_streaming="immediate")
    subscriber.append_conf(log_min_messages="warning")
    subscriber.pg_ctl("reload")
    subscriber.sql("SELECT 1")  # ensure the reload took effect

    offset = subscriber.current_log_position()
    publisher.sql("INSERT INTO test_tab_2 SELECT i FROM generate_series(1, 5000) s(i)")
    subscriber.wait_for_log(
        r"LOG: ( [A-Z0-9]+:)? logical replication apply worker will serialize the "
        r"remaining changes of remote transaction \d+ to a file",
        offset,
    )
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT count(*) FROM test_tab_2") == 15000, (
        "parallel apply worker replayed all changes from file"
    )
