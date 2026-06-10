# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/011_generated.pl.

Test generated columns under logical replication: stored/virtual generated
columns recompute locally on the subscriber, a subscriber-side replica trigger
fires, the publish_generated_columns option ('none'/'stored') controls whether
generated data is sent, column lists take precedence over that option, and
replicating into a generated subscriber column reports an error.
"""


def test_generated(create_pg):
    publisher = create_pg("publisher", allows_streaming="logical")
    subscriber = create_pg("subscriber")
    connstr = publisher.connstr()

    publisher.sql(
        "CREATE TABLE tab1 (a int PRIMARY KEY, "
        "b int GENERATED ALWAYS AS (a * 2) STORED, "
        "c int GENERATED ALWAYS AS (a * 3) VIRTUAL)"
    )
    subscriber.sql(
        "CREATE TABLE tab1 (a int PRIMARY KEY, "
        "b int GENERATED ALWAYS AS (a * 22) STORED, "
        "c int GENERATED ALWAYS AS (a * 33) VIRTUAL, d int)"
    )
    publisher.sql("INSERT INTO tab1 (a) VALUES (1), (2), (3)")

    publisher.sql("CREATE PUBLICATION pub1 FOR ALL TABLES")
    subscriber.sql(f"CREATE SUBSCRIPTION sub1 CONNECTION '{connstr}' PUBLICATION pub1")
    subscriber.wait_for_subscription_sync()

    assert subscriber.sql("SELECT a, b, c FROM tab1 ORDER BY a") == [
        (1, 22, 33),
        (2, 44, 66),
        (3, 66, 99),
    ], "generated columns initial sync"

    publisher.sql("INSERT INTO tab1 VALUES (4), (5)")
    publisher.sql("UPDATE tab1 SET a = 6 WHERE a = 5")
    publisher.wait_for_catchup("sub1")
    assert subscriber.sql("SELECT * FROM tab1 ORDER BY a") == [
        (1, 22, 33, None),
        (2, 44, 66, None),
        (3, 66, 99, None),
        (4, 88, 132, None),
        (6, 132, 198, None),
    ], "generated columns replicated"

    # A subscriber-side replica trigger fills the extra column 'd'.
    subscriber.sql_batch(
        """
        CREATE FUNCTION tab1_trigger_func() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          NEW.d := NEW.a + 10;
          RETURN NEW;
        END $$
        """,
        """
        CREATE TRIGGER test1 BEFORE INSERT OR UPDATE ON tab1
          FOR EACH ROW EXECUTE PROCEDURE tab1_trigger_func()
        """,
        "ALTER TABLE tab1 ENABLE REPLICA TRIGGER test1",
    )
    publisher.sql("INSERT INTO tab1 VALUES (7), (8)")
    publisher.sql("UPDATE tab1 SET a = 9 WHERE a = 7")
    publisher.wait_for_catchup("sub1")
    assert subscriber.sql("SELECT * FROM tab1 ORDER BY 1") == [
        (1, 22, 33, None),
        (2, 44, 66, None),
        (3, 66, 99, None),
        (4, 88, 132, None),
        (6, 132, 198, None),
        (8, 176, 264, 18),
        (9, 198, 297, 19),
    ], "generated columns replicated with trigger"

    subscriber.sql("DROP SUBSCRIPTION sub1")
    publisher.sql("DROP PUBLICATION pub1")

    # --- generated -> regular column with publish_generated_columns ----------
    subscriber.sql("CREATE DATABASE test_pgc_true")
    publisher.sql_batch(
        "CREATE TABLE tab_gen_to_nogen (a int, b int GENERATED ALWAYS AS (a * 2) STORED)",
        "INSERT INTO tab_gen_to_nogen (a) VALUES (1), (2), (3)",
        """
        CREATE PUBLICATION regress_pub1_gen_to_nogen FOR TABLE tab_gen_to_nogen
            WITH (publish_generated_columns = none)
        """,
        """
        CREATE PUBLICATION regress_pub2_gen_to_nogen FOR TABLE tab_gen_to_nogen
            WITH (publish_generated_columns = stored)
        """,
    )
    subscriber.sql("CREATE TABLE tab_gen_to_nogen (a int, b int)")
    subscriber.sql(
        f"CREATE SUBSCRIPTION regress_sub1_gen_to_nogen CONNECTION '{connstr}' "
        "PUBLICATION regress_pub1_gen_to_nogen WITH (copy_data = true)"
    )
    subscriber.sql_oneshot(
        "CREATE TABLE tab_gen_to_nogen (a int, b int)", dbname="test_pgc_true"
    )
    subscriber.sql_oneshot(
        f"CREATE SUBSCRIPTION regress_sub2_gen_to_nogen CONNECTION '{connstr}' "
        "PUBLICATION regress_pub2_gen_to_nogen WITH (copy_data = true)",
        dbname="test_pgc_true",
    )
    subscriber.wait_for_subscription_sync(publisher, "regress_sub1_gen_to_nogen")
    subscriber.wait_for_subscription_sync(
        publisher, "regress_sub2_gen_to_nogen", "test_pgc_true"
    )

    # 'none' doesn't copy the generated column; 'stored' does.
    assert subscriber.sql("SELECT a, b FROM tab_gen_to_nogen ORDER BY a") == [
        (1, None),
        (2, None),
        (3, None),
    ], "tab_gen_to_nogen initial sync, publish_generated_columns=none"
    assert subscriber.sql_oneshot(
        "SELECT a, b FROM tab_gen_to_nogen ORDER BY a", dbname="test_pgc_true"
    ) == [(1, 2), (2, 4), (3, 6)], (
        "tab_gen_to_nogen initial sync, publish_generated_columns=stored"
    )

    publisher.sql("INSERT INTO tab_gen_to_nogen VALUES (4), (5)")
    publisher.wait_for_catchup("regress_sub1_gen_to_nogen")
    assert subscriber.sql("SELECT a, b FROM tab_gen_to_nogen ORDER BY a") == [
        (1, None),
        (2, None),
        (3, None),
        (4, None),
        (5, None),
    ], "tab_gen_to_nogen incremental, publish_generated_columns=none"
    publisher.wait_for_catchup("regress_sub2_gen_to_nogen")
    assert subscriber.sql_oneshot(
        "SELECT a, b FROM tab_gen_to_nogen ORDER BY a", dbname="test_pgc_true"
    ) == [(1, 2), (2, 4), (3, 6), (4, 8), (5, 10)], (
        "tab_gen_to_nogen incremental, publish_generated_columns=stored"
    )

    subscriber.sql("DROP SUBSCRIPTION regress_sub1_gen_to_nogen")
    subscriber.sql_oneshot(
        "DROP SUBSCRIPTION regress_sub2_gen_to_nogen", dbname="test_pgc_true"
    )
    publisher.sql_batch(
        "DROP PUBLICATION regress_pub1_gen_to_nogen",
        "DROP PUBLICATION regress_pub2_gen_to_nogen",
    )
    subscriber.sql_oneshot("DROP table tab_gen_to_nogen", dbname="test_pgc_true")
    subscriber.sql("DROP DATABASE test_pgc_true")

    # --- column list takes precedence over publish_generated_columns=none ----
    publisher.sql_batch(
        "CREATE TABLE tab2 (a int, gen1 int GENERATED ALWAYS AS (a * 2) STORED)",
        "INSERT INTO tab2 (a) VALUES (1), (2)",
        "CREATE PUBLICATION pub1 FOR table tab2(gen1) WITH (publish_generated_columns=none)",
    )
    subscriber.sql("CREATE TABLE tab2 (a int, gen1 int)")
    subscriber.sql(
        f"CREATE SUBSCRIPTION sub1 CONNECTION '{connstr}' PUBLICATION pub1 "
        "WITH (copy_data = true)"
    )
    subscriber.wait_for_subscription_sync(publisher, "sub1")
    assert subscriber.sql("SELECT * FROM tab2 ORDER BY gen1") == [
        (None, 2),
        (None, 4),
    ], "tab2 initial sync, publish_generated_columns=none"
    publisher.sql("INSERT INTO tab2 VALUES (3), (4)")
    publisher.wait_for_catchup("sub1")
    assert subscriber.sql("SELECT * FROM tab2 ORDER BY gen1") == [
        (None, 2),
        (None, 4),
        (None, 6),
        (None, 8),
    ], "tab2 incremental, publish_generated_columns=none"
    subscriber.sql("DROP SUBSCRIPTION sub1")
    publisher.sql("DROP PUBLICATION pub1")

    # --- column list limits stored generated columns even with =stored -------
    publisher.sql_batch(
        """
        CREATE TABLE tab3 (a int, gen1 int GENERATED ALWAYS AS (a * 2) STORED,
            gen2 int GENERATED ALWAYS AS (a * 2) STORED)
        """,
        "INSERT INTO tab3 (a) VALUES (1), (2)",
        "CREATE PUBLICATION pub1 FOR table tab3(gen1) WITH (publish_generated_columns=stored)",
    )
    subscriber.sql("CREATE TABLE tab3 (a int, gen1 int, gen2 int)")
    subscriber.sql(
        f"CREATE SUBSCRIPTION sub1 CONNECTION '{connstr}' PUBLICATION pub1 "
        "WITH (copy_data = true)"
    )
    subscriber.wait_for_subscription_sync(publisher, "sub1")
    assert subscriber.sql("SELECT * FROM tab3 ORDER BY gen1") == [
        (None, 2, None),
        (None, 4, None),
    ], "tab3 initial sync, publish_generated_columns=stored"
    publisher.sql("INSERT INTO tab3 VALUES (3), (4)")
    publisher.wait_for_catchup("sub1")
    assert subscriber.sql("SELECT * FROM tab3 ORDER BY gen1") == [
        (None, 2, None),
        (None, 4, None),
        (None, 6, None),
        (None, 8, None),
    ], "tab3 incremental, publish_generated_columns=stored"
    subscriber.sql("DROP SUBSCRIPTION sub1")
    publisher.sql("DROP PUBLICATION pub1")

    # --- replicating into a generated subscriber column errors ---------------
    publisher.sql_batch(
        "CREATE TABLE t1(c1 int, c2 int, c3 int GENERATED ALWAYS AS (c1 * 2) STORED)",
        "CREATE PUBLICATION pub1 for table t1(c1, c2, c3)",
        "INSERT INTO t1 VALUES (1)",
    )
    subscriber.sql(
        "CREATE TABLE t1(c1 int, c2 int GENERATED ALWAYS AS (c1 + 2) STORED, "
        "c3 int GENERATED ALWAYS AS (c1 + 2) STORED)"
    )
    subscriber.sql(f"CREATE SUBSCRIPTION sub1 CONNECTION '{connstr}' PUBLICATION pub1")
    offset = subscriber.current_log_position()
    subscriber.wait_for_log(
        r'ERROR: ( [A-Z0-9]+:)? logical replication target relation "public.t1" '
        r'has incompatible generated columns: "c2", "c3"',
        offset,
    )
    subscriber.sql("DROP SUBSCRIPTION sub1")
    publisher.sql("DROP PUBLICATION pub1")
