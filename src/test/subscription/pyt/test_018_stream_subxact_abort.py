# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/018_stream_subxact_abort.pl.

Test streaming of transactions containing multiple subtransactions and
rollbacks (savepoint rollback, out-of-order RELEASE/ROLLBACK, full rollback),
for streaming='on' and streaming='parallel', plus serialize-to-file behaviour
of the parallel apply worker.
"""


def test_stream_subxact_abort(create_pg):
    publisher = create_pg(
        "publisher",
        allows_streaming="logical",
        conf={"debug_logical_replication_streaming": "immediate"},
    )
    subscriber = create_pg("subscriber")

    publisher.sql("CREATE TABLE test_tab (a int primary key, b bytea)")
    publisher.sql("INSERT INTO test_tab VALUES (1, 'foo'), (2, 'bar')")
    publisher.sql("CREATE TABLE test_tab_2 (a int)")
    subscriber.sql(
        "CREATE TABLE test_tab (a int primary key, b text, c INT, d INT, e INT)"
    )
    subscriber.sql("CREATE TABLE test_tab_2 (a int)")

    publisher.sql("CREATE PUBLICATION tap_pub FOR TABLE test_tab, test_tab_2")
    subscriber.sql(
        f"CREATE SUBSCRIPTION tap_sub CONNECTION '{publisher.connstr()} "
        "application_name=tap_sub' PUBLICATION tap_pub WITH (streaming = on)"
    )
    subscriber.wait_for_subscription_sync(publisher, "tap_sub")
    assert subscriber.sql("SELECT count(*), count(c) FROM test_tab") == (2, 0), (
        "check initial data was copied to subscriber"
    )

    def count_tab():
        return subscriber.sql("SELECT count(*), count(c) FROM test_tab")

    def test_streaming(is_parallel):
        offset = subscriber.current_log_position()
        publisher.sql_batch(
            "BEGIN",
            "INSERT INTO test_tab VALUES (3, sha256(3::text::bytea))",
            "SAVEPOINT s1",
            "INSERT INTO test_tab VALUES (4, sha256(4::text::bytea))",
            "SAVEPOINT s2",
            "INSERT INTO test_tab VALUES (5, sha256(5::text::bytea))",
            "SAVEPOINT s3",
            "INSERT INTO test_tab VALUES (6, sha256(6::text::bytea))",
            "ROLLBACK TO s2",
            "INSERT INTO test_tab VALUES (7, sha256(7::text::bytea))",
            "ROLLBACK TO s1",
            "INSERT INTO test_tab VALUES (8, sha256(8::text::bytea))",
            "SAVEPOINT s4",
            "INSERT INTO test_tab VALUES (9, sha256(9::text::bytea))",
            "SAVEPOINT s5",
            "INSERT INTO test_tab VALUES (10, sha256(10::text::bytea))",
            "COMMIT",
        )
        publisher.wait_for_catchup("tap_sub")
        if is_parallel:
            subscriber.wait_for_log(
                r"DEBUG: ( [A-Z0-9]+:)? finished processing the STREAM COMMIT command",
                offset,
            )
        assert count_tab() == (6, 0), (
            "check rollback to savepoint reflected and extra columns contain local defaults"
        )

        offset = subscriber.current_log_position()
        publisher.sql_batch(
            "BEGIN",
            "INSERT INTO test_tab VALUES (11, sha256(11::text::bytea))",
            "SAVEPOINT s1",
            "INSERT INTO test_tab VALUES (12, sha256(12::text::bytea))",
            "SAVEPOINT s2",
            "INSERT INTO test_tab VALUES (13, sha256(13::text::bytea))",
            "SAVEPOINT s3",
            "INSERT INTO test_tab VALUES (14, sha256(14::text::bytea))",
            "RELEASE s2",
            "INSERT INTO test_tab VALUES (15, sha256(15::text::bytea))",
            "ROLLBACK TO s1",
            "COMMIT",
        )
        publisher.wait_for_catchup("tap_sub")
        if is_parallel:
            subscriber.wait_for_log(
                r"DEBUG: ( [A-Z0-9]+:)? finished processing the STREAM COMMIT command",
                offset,
            )
        assert count_tab() == (7, 0), (
            "check rollback to savepoint was reflected on subscriber"
        )

        offset = subscriber.current_log_position()
        publisher.sql_batch(
            "BEGIN",
            "INSERT INTO test_tab VALUES (16, sha256(16::text::bytea))",
            "SAVEPOINT s1",
            "INSERT INTO test_tab VALUES (17, sha256(17::text::bytea))",
            "SAVEPOINT s2",
            "INSERT INTO test_tab VALUES (18, sha256(18::text::bytea))",
            "ROLLBACK",
        )
        publisher.wait_for_catchup("tap_sub")
        if is_parallel:
            subscriber.wait_for_log(
                r"DEBUG: ( [A-Z0-9]+:)? finished processing the STREAM ABORT command",
                offset,
            )
        assert count_tab() == (7, 0), "check rollback was reflected on subscriber"

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
    subscriber.append_conf(log_min_messages="debug1")
    subscriber.pg_ctl("reload")
    subscriber.sql("SELECT 1")
    test_streaming(True)

    # Serialize changes to files and apply at transaction end.
    subscriber.append_conf(debug_logical_replication_streaming="immediate")
    subscriber.append_conf(log_min_messages="warning")
    subscriber.pg_ctl("reload")
    subscriber.sql("SELECT 1")

    serialize_msg = (
        r"LOG: ( [A-Z0-9]+:)? logical replication apply worker will serialize the "
        r"remaining changes of remote transaction \d+ to a file"
    )

    offset = subscriber.current_log_position()
    publisher.sql_batch("BEGIN", "INSERT INTO test_tab_2 values(1)", "ROLLBACK")
    subscriber.wait_for_log(serialize_msg, offset)
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT count(*) FROM test_tab_2") == 0, (
        "check rollback was reflected on subscriber"
    )

    offset = subscriber.current_log_position()
    publisher.sql_batch(
        "BEGIN",
        "INSERT INTO test_tab_2 values(1)",
        "SAVEPOINT sp",
        "INSERT INTO test_tab_2 values(1)",
        "ROLLBACK TO sp",
        "COMMIT",
    )
    subscriber.wait_for_log(serialize_msg, offset)
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT count(*) FROM test_tab_2") == 1, (
        "check rollback to savepoint was reflected on subscriber"
    )
