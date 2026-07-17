# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/016_stream_subxact.pl.

Test streaming of a transaction containing subtransactions, for both
streaming='on' and streaming='parallel'; in the parallel case also confirm the
parallel apply worker finished via the subscriber's DEBUG log.
"""


def test_stream_subxact(create_pg):
    publisher = create_pg(
        "publisher",
        allows_streaming="logical",
        conf={"debug_logical_replication_streaming": "immediate"},
    )
    subscriber = create_pg("subscriber")

    publisher.sql("CREATE TABLE test_tab (a int primary key, b bytea)")
    publisher.sql("INSERT INTO test_tab VALUES (1, 'foo'), (2, 'bar')")
    subscriber.sql(
        "CREATE TABLE test_tab (a int primary key, b bytea, "
        "c timestamptz DEFAULT now(), d bigint DEFAULT 999)"
    )

    publisher.sql("CREATE PUBLICATION tap_pub FOR TABLE test_tab")
    subscriber.sql(
        f"CREATE SUBSCRIPTION tap_sub CONNECTION '{publisher.connstr()} "
        "application_name=tap_sub' PUBLICATION tap_pub WITH (streaming = on)"
    )
    subscriber.wait_for_subscription_sync(publisher, "tap_sub")
    assert subscriber.sql(
        "SELECT count(*), count(c), count(d = 999) FROM test_tab"
    ) == (2, 2, 2), "check initial data was copied to subscriber"

    def test_streaming(is_parallel):
        offset = subscriber.current_log_position()
        publisher.sql_batch(
            "BEGIN",
            "INSERT INTO test_tab SELECT i, sha256(i::text::bytea) FROM generate_series(3, 5) s(i)",
            "UPDATE test_tab SET b = sha256(b) WHERE mod(a,2) = 0",
            "DELETE FROM test_tab WHERE mod(a,3) = 0",
            "SAVEPOINT s1",
            "INSERT INTO test_tab SELECT i, sha256(i::text::bytea) FROM generate_series(6, 8) s(i)",
            "UPDATE test_tab SET b = sha256(b) WHERE mod(a,2) = 0",
            "DELETE FROM test_tab WHERE mod(a,3) = 0",
            "SAVEPOINT s2",
            "INSERT INTO test_tab SELECT i, sha256(i::text::bytea) FROM generate_series(9, 11) s(i)",
            "UPDATE test_tab SET b = sha256(b) WHERE mod(a,2) = 0",
            "DELETE FROM test_tab WHERE mod(a,3) = 0",
            "SAVEPOINT s3",
            "INSERT INTO test_tab SELECT i, sha256(i::text::bytea) FROM generate_series(12, 14) s(i)",
            "UPDATE test_tab SET b = sha256(b) WHERE mod(a,2) = 0",
            "DELETE FROM test_tab WHERE mod(a,3) = 0",
            "SAVEPOINT s4",
            "INSERT INTO test_tab SELECT i, sha256(i::text::bytea) FROM generate_series(15, 17) s(i)",
            "UPDATE test_tab SET b = sha256(b) WHERE mod(a,2) = 0",
            "DELETE FROM test_tab WHERE mod(a,3) = 0",
            "COMMIT",
        )
        publisher.wait_for_catchup("tap_sub")
        if is_parallel:
            subscriber.wait_for_log(
                r"DEBUG: ( [A-Z0-9]+:)? finished processing the STREAM COMMIT command",
                offset,
            )
        assert subscriber.sql(
            "SELECT count(*), count(c), count(d = 999) FROM test_tab"
        ) == (12, 12, 12), (
            "data copied in streaming mode and extra columns contain local defaults"
        )
        publisher.sql("DELETE FROM test_tab WHERE (a > 2)")
        publisher.wait_for_catchup("tap_sub")

    # streaming = 'on'
    test_streaming(False)

    # streaming = 'parallel'
    oldpid = publisher.sql(
        "SELECT pid FROM pg_stat_replication "
        "WHERE application_name = 'tap_sub' AND state = 'streaming'"
    )
    subscriber.sql("ALTER SUBSCRIPTION tap_sub SET(streaming = parallel)")
    publisher.poll_query_until(
        f"SELECT pid != {oldpid} FROM pg_stat_replication "
        "WHERE application_name = 'tap_sub' AND state = 'streaming'"
    )
    # Bump log verbosity so the parallel apply worker's DEBUG line is visible.
    subscriber.append_conf(log_min_messages="debug1")
    subscriber.pg_ctl("reload")
    subscriber.sql("SELECT 1")
    test_streaming(True)
