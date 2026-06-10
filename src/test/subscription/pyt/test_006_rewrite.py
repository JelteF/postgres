# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/006_rewrite.pl.

Test logical replication behavior across a heap rewrite (ALTER TABLE ADD column
with a NOT NULL DEFAULT) on both publisher and subscriber.
"""


def test_rewrite(create_pg):
    publisher = create_pg("publisher", allows_streaming="logical")
    subscriber = create_pg("subscriber")

    ddl = "CREATE TABLE test1 (a int, b text)"
    publisher.sql(ddl)
    subscriber.sql(ddl)

    publisher.sql("CREATE PUBLICATION mypub FOR ALL TABLES")
    subscriber.sql(
        f"CREATE SUBSCRIPTION mysub CONNECTION '{publisher.connstr()}' PUBLICATION mypub"
    )
    subscriber.wait_for_subscription_sync(publisher, "mysub")

    publisher.sql("INSERT INTO test1 (a, b) VALUES (1, 'one'), (2, 'two')")
    publisher.wait_for_catchup("mysub")
    assert subscriber.sql("SELECT a, b FROM test1 ORDER BY a") == [
        (1, "one"),
        (2, "two"),
    ], "initial data replicated to subscriber"

    # DDL that causes a heap rewrite, applied on both nodes.
    ddl2 = "ALTER TABLE test1 ADD c int NOT NULL DEFAULT 0"
    subscriber.sql(ddl2)
    publisher.sql(ddl2)
    publisher.wait_for_catchup("mysub")

    publisher.sql("INSERT INTO test1 (a, b, c) VALUES (3, 'three', 33)")
    publisher.wait_for_catchup("mysub")
    assert subscriber.sql("SELECT a, b, c FROM test1 ORDER BY a") == [
        (1, "one", 0),
        (2, "two", 0),
        (3, "three", 33),
    ], "data replicated to subscriber"
