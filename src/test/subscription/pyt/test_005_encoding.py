# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/005_encoding.pl.

Test logical replication between databases with different encodings: a UTF8
publisher to a LATIN1 subscriber, confirming the data is recoded.
"""


def test_encoding(create_pg):
    publisher = create_pg(
        "publisher",
        allows_streaming="logical",
        initdb_opts=["--locale=C", "--encoding=UTF8"],
    )
    subscriber = create_pg(
        "subscriber", initdb_opts=["--locale=C", "--encoding=LATIN1"]
    )

    ddl = "CREATE TABLE test1 (a int, b text)"
    publisher.sql(ddl)
    subscriber.sql(ddl)

    publisher.sql("CREATE PUBLICATION mypub FOR ALL TABLES")
    subscriber.sql(
        f"CREATE SUBSCRIPTION mysub CONNECTION '{publisher.connstr()}' PUBLICATION mypub"
    )
    subscriber.wait_for_subscription_sync(publisher, "mysub")

    # hand-rolled UTF-8 bytes for "Motörhead"
    publisher.sql(r"INSERT INTO test1 VALUES (1, E'Mot\xc3\xb6rhead')")
    publisher.wait_for_catchup("mysub")

    # LATIN1 byte for ö
    assert subscriber.sql(r"SELECT a FROM test1 WHERE b = E'Mot\xf6rhead'") == 1, (
        "data replicated to subscriber"
    )
