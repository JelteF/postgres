# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/019_stream_subxact_ddl_abort.pl.

Test streaming of a transaction with subtransactions, DDLs, DMLs and rollbacks
(the publisher-side DDL/DML interaction), checking that the rollback-to-
savepoint is reflected on the subscriber and extra columns keep local defaults.
"""


def test_stream_subxact_ddl_abort(create_pg):
    publisher = create_pg(
        "publisher",
        allows_streaming="logical",
        conf={"debug_logical_replication_streaming": "immediate"},
    )
    subscriber = create_pg("subscriber")

    publisher.sql("CREATE TABLE test_tab (a int primary key, b bytea)")
    publisher.sql("INSERT INTO test_tab VALUES (1, 'foo'), (2, 'bar')")
    subscriber.sql(
        "CREATE TABLE test_tab (a int primary key, b bytea, c INT, d INT, e INT)"
    )

    publisher.sql("CREATE PUBLICATION tap_pub FOR TABLE test_tab")
    subscriber.sql(
        f"CREATE SUBSCRIPTION tap_sub CONNECTION '{publisher.connstr()} "
        "application_name=tap_sub' PUBLICATION tap_pub WITH (streaming = on)"
    )
    subscriber.wait_for_subscription_sync(publisher, "tap_sub")

    assert subscriber.sql("SELECT count(*), count(c) FROM test_tab") == (2, 0), (
        "check initial data was copied to subscriber"
    )

    # A streamed transaction with DDL, DML and ROLLBACKs. The explicit
    # BEGIN..COMMIT (with savepoints) runs as one simple-query message.
    publisher.sql_batch(
        "BEGIN",
        "INSERT INTO test_tab VALUES (3, sha256(3::text::bytea))",
        "ALTER TABLE test_tab ADD COLUMN c INT",
        "SAVEPOINT s1",
        "INSERT INTO test_tab VALUES (4, sha256(4::text::bytea), -4)",
        "ALTER TABLE test_tab ADD COLUMN d INT",
        "SAVEPOINT s2",
        "INSERT INTO test_tab VALUES (5, sha256(5::text::bytea), -5, 5*2)",
        "ALTER TABLE test_tab ADD COLUMN e INT",
        "SAVEPOINT s3",
        "INSERT INTO test_tab VALUES (6, sha256(6::text::bytea), -6, 6*2, -6*3)",
        "ALTER TABLE test_tab DROP COLUMN c",
        "ROLLBACK TO s1",
        "INSERT INTO test_tab VALUES (4, sha256(4::text::bytea), 4)",
        "COMMIT",
    )
    publisher.wait_for_catchup("tap_sub")

    assert subscriber.sql("SELECT count(*), count(c) FROM test_tab") == (4, 1), (
        "rollback to savepoint reflected and extra columns contain local defaults"
    )
