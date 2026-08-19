# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/017_stream_ddl.pl.

Test streaming of large transactions that mix DDL and DML with subtransactions
on the publisher side, including a DDL after DML (forcing the cached schema to
be re-sent), confirming the subscriber's extra columns keep local defaults.
"""


def test_stream_ddl(create_pg):
    publisher = create_pg(
        "publisher",
        allows_streaming="logical",
        conf={"logical_decoding_work_mem": "64kB"},
    )
    subscriber = create_pg("subscriber")

    publisher.sql("CREATE TABLE test_tab (a int primary key, b varchar)")
    publisher.sql("INSERT INTO test_tab VALUES (1, 'foo'), (2, 'bar')")
    subscriber.sql(
        "CREATE TABLE test_tab (a int primary key, b bytea, c INT, d INT, e INT, f INT)"
    )

    publisher.sql("CREATE PUBLICATION tap_pub FOR TABLE test_tab")
    subscriber.sql(
        f"CREATE SUBSCRIPTION tap_sub CONNECTION '{publisher.connstr()} "
        "application_name=tap_sub' PUBLICATION tap_pub WITH (streaming = on)"
    )
    subscriber.wait_for_subscription_sync(publisher, "tap_sub")
    assert subscriber.sql(
        "SELECT count(*), count(c), count(d = 999) FROM test_tab"
    ) == (2, 0, 0), "check initial data was copied to subscriber"

    # A small (non-streamed) transaction with DDL and DML.
    publisher.sql_batch(
        "BEGIN",
        "INSERT INTO test_tab VALUES (3, sha256(3::text::bytea))",
        "ALTER TABLE test_tab ADD COLUMN c INT",
        "SAVEPOINT s1",
        "INSERT INTO test_tab VALUES (4, sha256(4::text::bytea), -4)",
        "COMMIT",
    )
    # A large (streamed) transaction with DDL and DML.
    publisher.sql_batch(
        "BEGIN",
        "INSERT INTO test_tab SELECT i, sha256(i::text::bytea), -i FROM generate_series(5, 1000) s(i)",
        "ALTER TABLE test_tab ADD COLUMN d INT",
        "SAVEPOINT s1",
        "INSERT INTO test_tab SELECT i, sha256(i::text::bytea), -i, 2*i FROM generate_series(1001, 2000) s(i)",
        "COMMIT",
    )
    # Another small transaction with DDL and DML.
    publisher.sql_batch(
        "BEGIN",
        "INSERT INTO test_tab VALUES (2001, sha256(2001::text::bytea), -2001, 2*2001)",
        "ALTER TABLE test_tab ADD COLUMN e INT",
        "SAVEPOINT s1",
        "INSERT INTO test_tab VALUES (2002, sha256(2002::text::bytea), -2002, 2*2002, -3*2002)",
        "COMMIT",
    )
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql(
        "SELECT count(*), count(c), count(d), count(e) FROM test_tab"
    ) == (2002, 1999, 1002, 1), (
        "data copied in streaming mode and extra columns contain local defaults"
    )

    # A large (streamed) transaction with a DDL after DML (invalidates the
    # cached schema so it must be re-sent).
    publisher.sql_batch(
        "BEGIN",
        "INSERT INTO test_tab SELECT i, sha256(i::text::bytea), -i, 2*i, -3*i FROM generate_series(2003,5000) s(i)",
        "ALTER TABLE test_tab ADD COLUMN f INT",
        "COMMIT",
    )
    # A small transaction to ensure the schema is sent again.
    publisher.sql_batch(
        "BEGIN",
        "INSERT INTO test_tab SELECT i, sha256(i::text::bytea), -i, 2*i, -3*i, 4*i FROM generate_series(5001,5005) s(i)",
        "COMMIT",
    )
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql(
        "SELECT count(*), count(c), count(d), count(e), count(f) FROM test_tab"
    ) == (5005, 5002, 4005, 3004, 5), (
        "data copied for both streaming and non-streaming transactions"
    )
