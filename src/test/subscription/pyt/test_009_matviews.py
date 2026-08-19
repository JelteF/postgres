# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/009_matviews.pl.

Materialized views are not supported by logical replication, but logical
decoding still produces change information for them, so the apply worker must
ignore it (bug #15044): creating an MV on the publisher with no matching
relation on the subscriber must not hang replication.
"""


def test_matviews(create_pg):
    publisher = create_pg("publisher", allows_streaming="logical")
    subscriber = create_pg("subscriber")

    publisher.sql("CREATE TABLE test1 (a int PRIMARY KEY, b text)")
    subscriber.sql("CREATE TABLE test1 (a int PRIMARY KEY, b text)")

    publisher.sql("CREATE PUBLICATION mypub FOR ALL TABLES")
    subscriber.sql(
        f"CREATE SUBSCRIPTION mysub CONNECTION '{publisher.connstr()}' PUBLICATION mypub"
    )

    publisher.sql("INSERT INTO test1 (a, b) VALUES (1, 'one'), (2, 'two')")
    publisher.wait_for_catchup("mysub")

    # Create an MV with data; its change info must be ignored by the apply
    # worker (there is no equivalent relation on the subscriber).
    publisher.sql("CREATE MATERIALIZED VIEW testmv1 AS SELECT * FROM test1")
    publisher.wait_for_catchup("mysub")
    # Reaching here (no hang) is the assertion: MV data is not replicated.
