# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/025_rep_changes_for_schema.pl.

Logical replication tests for FOR TABLES IN SCHEMA publications: initial sync
and incremental changes of schema tables (including partitioned), that new/
moved/dropped tables are picked up only after REFRESH PUBLICATION, and that
dropping the schema from the publication stops replication of its tables.
"""


def test_rep_changes_for_schema(create_pg):
    publisher = create_pg("publisher", allows_streaming="logical")
    subscriber = create_pg("subscriber")
    connstr = publisher.connstr()

    publisher.sql("CREATE SCHEMA sch1")
    publisher.sql("CREATE TABLE sch1.tab1 AS SELECT generate_series(1,10) AS a")
    publisher.sql("CREATE TABLE sch1.tab2 AS SELECT generate_series(1,10) AS a")
    publisher.sql(
        "CREATE TABLE sch1.tab1_parent (a int PRIMARY KEY, b text) PARTITION BY LIST (a)"
    )
    publisher.sql(
        "CREATE TABLE public.tab1_child1 PARTITION OF sch1.tab1_parent FOR VALUES IN (1, 2, 3)"
    )
    publisher.sql(
        "CREATE TABLE public.tab1_child2 PARTITION OF sch1.tab1_parent FOR VALUES IN (4, 5, 6)"
    )
    publisher.sql("INSERT INTO sch1.tab1_parent values (1),(4)")

    subscriber.sql("CREATE SCHEMA sch1")
    subscriber.sql("CREATE TABLE sch1.tab1 (a int)")
    subscriber.sql("CREATE TABLE sch1.tab2 (a int)")
    subscriber.sql(
        "CREATE TABLE sch1.tab1_parent (a int PRIMARY KEY, b text) PARTITION BY LIST (a)"
    )
    subscriber.sql(
        "CREATE TABLE public.tab1_child1 PARTITION OF sch1.tab1_parent FOR VALUES IN (1, 2, 3)"
    )
    subscriber.sql(
        "CREATE TABLE public.tab1_child2 PARTITION OF sch1.tab1_parent FOR VALUES IN (4, 5, 6)"
    )

    publisher.sql("CREATE PUBLICATION tap_pub_schema FOR TABLES IN SCHEMA sch1")
    subscriber.sql(
        f"CREATE SUBSCRIPTION tap_sub_schema CONNECTION '{connstr}' "
        "PUBLICATION tap_pub_schema"
    )
    subscriber.wait_for_subscription_sync(publisher, "tap_sub_schema")

    assert subscriber.sql("SELECT count(*), min(a), max(a) FROM sch1.tab1") == (
        10,
        1,
        10,
    ), "check rows on subscriber catchup"
    assert subscriber.sql("SELECT count(*), min(a), max(a) FROM sch1.tab2") == (
        10,
        1,
        10,
    ), "check rows on subscriber catchup"
    assert subscriber.sql("SELECT * FROM sch1.tab1_parent order by 1") == [
        (1, None),
        (4, None),
    ], "check rows on subscriber catchup"

    publisher.sql("INSERT INTO sch1.tab1 VALUES(generate_series(11,20))")
    publisher.sql("INSERT INTO sch1.tab1_parent values (2),(5)")
    publisher.wait_for_catchup("tap_sub_schema")
    assert subscriber.sql("SELECT count(*), min(a), max(a) FROM sch1.tab1") == (
        20,
        1,
        20,
    ), "check replicated inserts on subscriber"
    assert subscriber.sql("SELECT * FROM sch1.tab1_parent order by 1") == [
        (1, None),
        (2, None),
        (4, None),
        (5, None),
    ], "check replicated inserts on subscriber"

    # A new table in the schema isn't synced until REFRESH PUBLICATION.
    publisher.sql("CREATE TABLE sch1.tab3 AS SELECT generate_series(1,10) AS a")
    subscriber.sql("CREATE TABLE sch1.tab3(a int)")
    publisher.wait_for_catchup("tap_sub_schema")
    assert subscriber.sql("SELECT count(*) FROM sch1.tab3") == 0, (
        "check replicated inserts on subscriber"
    )

    subscriber.sql("ALTER SUBSCRIPTION tap_sub_schema REFRESH PUBLICATION")
    subscriber.wait_for_subscription_sync()
    publisher.sql("INSERT INTO sch1.tab3 VALUES(11)")
    publisher.wait_for_catchup("tap_sub_schema")
    assert subscriber.sql("SELECT count(*), min(a), max(a) FROM sch1.tab3") == (
        11,
        1,
        11,
    ), "check rows on subscriber catchup"

    # Moving a table out of the schema stops its replication.
    publisher.sql("ALTER TABLE sch1.tab3 SET SCHEMA public")
    publisher.sql("INSERT INTO public.tab3 VALUES(12)")
    publisher.wait_for_catchup("tap_sub_schema")
    assert subscriber.sql("SELECT count(*), min(a), max(a) FROM sch1.tab3") == (
        11,
        1,
        11,
    ), "check replicated inserts on subscriber"

    relcount = (
        "SELECT count(*) FROM pg_subscription_rel WHERE srsubid IN "
        "(SELECT oid FROM pg_subscription WHERE subname = 'tap_sub_schema')"
    )
    assert subscriber.sql(relcount) == 5, (
        "check subscription relation status is not yet dropped on subscriber"
    )
    subscriber.sql("ALTER SUBSCRIPTION tap_sub_schema REFRESH PUBLICATION")
    subscriber.wait_for_subscription_sync()
    assert subscriber.sql(relcount) == 4, (
        "check subscription relation status was dropped on subscriber"
    )

    # Dropping a table from the schema removes it after refresh.
    publisher.sql("DROP TABLE sch1.tab2")
    publisher.wait_for_catchup("tap_sub_schema")
    assert subscriber.sql(relcount) == 4, (
        "check subscription relation status is not yet dropped on subscriber"
    )
    subscriber.sql("ALTER SUBSCRIPTION tap_sub_schema REFRESH PUBLICATION")
    subscriber.wait_for_subscription_sync()
    assert subscriber.sql(relcount) == 3, (
        "check subscription relation status was dropped on subscriber"
    )

    # Dropping the schema from the publication stops replication of its tables.
    publisher.sql_batch(
        "INSERT INTO sch1.tab1 VALUES(21)",
        "ALTER PUBLICATION tap_pub_schema DROP TABLES IN SCHEMA sch1",
        "INSERT INTO sch1.tab1 values(22)",
    )
    publisher.wait_for_catchup("tap_sub_schema")
    assert subscriber.sql("SELECT count(*), min(a), max(a) FROM sch1.tab1") == (
        21,
        1,
        21,
    ), "check replicated inserts on subscriber"
