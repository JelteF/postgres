# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/010_truncate.pl.

Test TRUNCATE replication: with/without RESTART IDENTITY, publications that do
or don't publish truncate, multiple tables joined by foreign keys, truncate of
mixed published/unpublished tables, synchronous logical replication, and
multiple subscriptions on one table.
"""


def test_truncate(create_pg):
    publisher = create_pg("publisher", allows_streaming="logical")
    subscriber = create_pg("subscriber", conf={"max_logical_replication_workers": 6})
    connstr = publisher.connstr()

    for tab in ("tab1", "tab2", "tab3"):
        publisher.sql(f"CREATE TABLE {tab} (a int PRIMARY KEY)")
        subscriber.sql(f"CREATE TABLE {tab} (a int PRIMARY KEY)")
    publisher.sql("CREATE TABLE tab4 (x int PRIMARY KEY, y int REFERENCES tab3)")
    subscriber.sql("CREATE TABLE tab4 (x int PRIMARY KEY, y int REFERENCES tab3)")

    subscriber.sql("CREATE SEQUENCE seq1 OWNED BY tab1.a")
    subscriber.sql("ALTER SEQUENCE seq1 START 101")

    publisher.sql("CREATE PUBLICATION pub1 FOR TABLE tab1")
    publisher.sql("CREATE PUBLICATION pub2 FOR TABLE tab2 WITH (publish = insert)")
    publisher.sql("CREATE PUBLICATION pub3 FOR TABLE tab3, tab4")
    subscriber.sql(f"CREATE SUBSCRIPTION sub1 CONNECTION '{connstr}' PUBLICATION pub1")
    subscriber.sql(f"CREATE SUBSCRIPTION sub2 CONNECTION '{connstr}' PUBLICATION pub2")
    subscriber.sql(f"CREATE SUBSCRIPTION sub3 CONNECTION '{connstr}' PUBLICATION pub3")
    subscriber.wait_for_subscription_sync()

    subscriber.sql("INSERT INTO tab1 VALUES (1), (2), (3)")
    publisher.wait_for_catchup("sub1")

    publisher.sql("TRUNCATE tab1")
    publisher.wait_for_catchup("sub1")
    assert subscriber.sql("SELECT count(*), min(a), max(a) FROM tab1") == (
        0,
        None,
        None,
    ), "truncate replicated"
    assert subscriber.sql("SELECT nextval('seq1')") == 1, "sequence not restarted"

    publisher.sql("TRUNCATE tab1 RESTART IDENTITY")
    publisher.wait_for_catchup("sub1")
    assert subscriber.sql("SELECT nextval('seq1')") == 101, (
        "truncate restarted identities"
    )

    # A publication that does not replicate truncate.
    subscriber.sql("INSERT INTO tab2 VALUES (1), (2), (3)")
    publisher.sql("TRUNCATE tab2")
    publisher.wait_for_catchup("sub2")
    assert subscriber.sql("SELECT count(*), min(a), max(a) FROM tab2") == (3, 1, 3), (
        "truncate not replicated"
    )

    publisher.sql("ALTER PUBLICATION pub2 SET (publish = 'insert, truncate')")
    publisher.sql("TRUNCATE tab2")
    publisher.wait_for_catchup("sub2")
    assert subscriber.sql("SELECT count(*), min(a), max(a) FROM tab2") == (
        0,
        None,
        None,
    ), "truncate replicated after publication change"

    # Multiple tables connected by foreign keys.
    subscriber.sql("INSERT INTO tab3 VALUES (1), (2), (3)")
    subscriber.sql("INSERT INTO tab4 VALUES (11, 1), (111, 1), (22, 2)")
    publisher.sql("TRUNCATE tab3, tab4")
    publisher.wait_for_catchup("sub3")
    assert subscriber.sql("SELECT count(*), min(a), max(a) FROM tab3") == (
        0,
        None,
        None,
    ), "truncate of multiple tables replicated"
    assert subscriber.sql("SELECT count(*), min(x), max(x) FROM tab4") == (
        0,
        None,
        None,
    ), "truncate of multiple tables replicated"

    # Truncate of multiple tables, some not published.
    subscriber.sql("DROP SUBSCRIPTION sub2")
    publisher.sql("DROP PUBLICATION pub2")
    subscriber.sql("INSERT INTO tab1 VALUES (1), (2), (3)")
    subscriber.sql("INSERT INTO tab2 VALUES (1), (2), (3)")
    publisher.sql("TRUNCATE tab1, tab2")
    publisher.wait_for_catchup("sub1")
    assert subscriber.sql("SELECT count(*), min(a), max(a) FROM tab1") == (
        0,
        None,
        None,
    ), "truncate of multiple tables some not published"
    assert subscriber.sql("SELECT count(*), min(a), max(a) FROM tab2") == (3, 1, 3), (
        "truncate of multiple tables some not published"
    )

    # Truncate under synchronous logical replication.
    publisher.sql("ALTER SYSTEM SET synchronous_standby_names TO 'sub1'")
    publisher.sql("SELECT pg_reload_conf()")
    publisher.sql("INSERT INTO tab1 VALUES (1), (2), (3)")
    publisher.wait_for_catchup("sub1")
    assert subscriber.sql("SELECT count(*), min(a), max(a) FROM tab1") == (3, 1, 3), (
        "check synchronous logical replication"
    )
    publisher.sql("TRUNCATE tab1")
    publisher.wait_for_catchup("sub1")
    assert subscriber.sql("SELECT count(*), min(a), max(a) FROM tab1") == (
        0,
        None,
        None,
    ), "truncate replicated in synchronous logical replication"
    publisher.sql("ALTER SYSTEM RESET synchronous_standby_names")
    publisher.sql("SELECT pg_reload_conf()")

    # Truncate with multiple subscriptions on a single table.
    publisher.sql("CREATE TABLE tab5 (a int)")
    subscriber.sql("CREATE TABLE tab5 (a int)")
    publisher.sql("CREATE PUBLICATION pub5 FOR TABLE tab5")
    subscriber.sql(
        f"CREATE SUBSCRIPTION sub5_1 CONNECTION '{connstr}' PUBLICATION pub5"
    )
    subscriber.sql(
        f"CREATE SUBSCRIPTION sub5_2 CONNECTION '{connstr}' PUBLICATION pub5"
    )
    subscriber.wait_for_subscription_sync()

    publisher.sql("INSERT INTO tab5 VALUES (1), (2), (3)")
    publisher.wait_for_catchup("sub5_1")
    publisher.wait_for_catchup("sub5_2")
    assert subscriber.sql("SELECT count(*), min(a), max(a) FROM tab5") == (6, 1, 3), (
        "insert replicated for multiple subscriptions"
    )
    publisher.sql("TRUNCATE tab5")
    publisher.wait_for_catchup("sub5_1")
    publisher.wait_for_catchup("sub5_2")
    assert subscriber.sql("SELECT count(*), min(a), max(a) FROM tab5") == (
        0,
        None,
        None,
    ), "truncate replicated for multiple subscriptions"

    assert (
        subscriber.sql(
            "SELECT deadlocks FROM pg_stat_database WHERE datname='postgres'"
        )
        == 0
    ), "no deadlocks detected"
