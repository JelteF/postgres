# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/028_row_filter.pl.

Tests logical replication row filtering: WHERE clauses on FOR TABLE
publications, the fact that FOR ALL TABLES / TABLES IN SCHEMA publications
ignore filters, OR'ing of filters when a table is in several publications,
filtering on partitions with and without publish_via_partition_root, on
inherited tables, on REPLICA IDENTITY FULL / index, on TOAST values, and on
virtual generated columns.
"""


def test_row_filter(create_pg):
    publisher = create_pg("publisher", allows_streaming="logical")
    subscriber = create_pg("subscriber")
    connstr = f"{publisher.connstr()} application_name=tap_sub"

    # ================================================================
    # FOR ALL TABLES: must come first so later test tables don't affect it.
    publisher.sql("CREATE TABLE tab_rf_x (x int primary key)")
    subscriber.sql("CREATE TABLE tab_rf_x (x int primary key)")
    publisher.sql("INSERT INTO tab_rf_x (x) VALUES (0), (5), (10), (15), (20)")
    publisher.sql("CREATE PUBLICATION tap_pub_x FOR TABLE tab_rf_x WHERE (x > 10)")
    publisher.sql("CREATE PUBLICATION tap_pub_forall FOR ALL TABLES")
    subscriber.sql(
        f"CREATE SUBSCRIPTION tap_sub CONNECTION '{connstr}' "
        "PUBLICATION tap_pub_x, tap_pub_forall"
    )
    subscriber.wait_for_subscription_sync(publisher, "tap_sub")

    # FOR ALL TABLES means no filtering on the tablesync COPY: all 5 present.
    assert subscriber.sql("SELECT count(x) FROM tab_rf_x") == 5, (
        "check initial data copy from table tab_rf_x should not be filtered"
    )

    # The tab_rf_x filter also has no effect when combined with ALL TABLES.
    # Expected: 5 initial rows + 2 new rows = 7 rows.
    publisher.sql("INSERT INTO tab_rf_x (x) VALUES (-99), (99)")
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT count(x) FROM tab_rf_x") == 7, (
        "check table tab_rf_x should not be filtered"
    )

    publisher.sql("DROP PUBLICATION tap_pub_forall")
    publisher.sql("DROP PUBLICATION tap_pub_x")
    publisher.sql("DROP TABLE tab_rf_x")
    subscriber.sql("DROP SUBSCRIPTION tap_sub")
    subscriber.sql("DROP TABLE tab_rf_x")

    # ================================================================
    # TABLES IN SCHEMA: independent, cleans up after itself.
    publisher.sql_batch(
        "CREATE SCHEMA schema_rf_x",
        "CREATE TABLE schema_rf_x.tab_rf_x (x int primary key)",
        "CREATE TABLE schema_rf_x.tab_rf_partitioned (x int primary key) PARTITION BY RANGE(x)",
        "CREATE TABLE public.tab_rf_partition (LIKE schema_rf_x.tab_rf_partitioned)",
        "ALTER TABLE schema_rf_x.tab_rf_partitioned ATTACH PARTITION public.tab_rf_partition DEFAULT",
    )
    subscriber.sql_batch(
        "CREATE SCHEMA schema_rf_x",
        "CREATE TABLE schema_rf_x.tab_rf_x (x int primary key)",
        "CREATE TABLE schema_rf_x.tab_rf_partitioned (x int primary key) PARTITION BY RANGE(x)",
        "CREATE TABLE public.tab_rf_partition (LIKE schema_rf_x.tab_rf_partitioned)",
        "ALTER TABLE schema_rf_x.tab_rf_partitioned ATTACH PARTITION public.tab_rf_partition DEFAULT",
    )
    publisher.sql(
        "INSERT INTO schema_rf_x.tab_rf_x (x) VALUES (0), (5), (10), (15), (20)"
    )
    publisher.sql("INSERT INTO schema_rf_x.tab_rf_partitioned (x) VALUES (1), (20)")
    publisher.sql(
        "CREATE PUBLICATION tap_pub_x FOR TABLE schema_rf_x.tab_rf_x WHERE (x > 10)"
    )
    publisher.sql(
        "CREATE PUBLICATION tap_pub_allinschema FOR TABLES IN SCHEMA schema_rf_x, "
        "TABLE schema_rf_x.tab_rf_x WHERE (x > 10)"
    )
    publisher.sql(
        "ALTER PUBLICATION tap_pub_allinschema ADD TABLE public.tab_rf_partition WHERE (x > 10)"
    )
    subscriber.sql(
        f"CREATE SUBSCRIPTION tap_sub CONNECTION '{connstr}' "
        "PUBLICATION tap_pub_x, tap_pub_allinschema"
    )
    subscriber.wait_for_subscription_sync(publisher, "tap_sub")

    # TABLES IN SCHEMA means no filtering on the tablesync COPY: all 5 present.
    assert subscriber.sql("SELECT count(x) FROM schema_rf_x.tab_rf_x") == 5, (
        "check initial data copy from table tab_rf_x should not be filtered"
    )

    # The tab_rf_x filter has no effect combined with TABLES IN SCHEMA, but the
    # tab_rf_partition filter does work since that partition is in a different
    # schema (and publish_via_partition_root = false).
    # Expected: tab_rf_x 5+2=7 rows; tab_rf_partition 1+1=2 rows.
    publisher.sql("INSERT INTO schema_rf_x.tab_rf_x (x) VALUES (-99), (99)")
    publisher.sql("INSERT INTO schema_rf_x.tab_rf_partitioned (x) VALUES (5), (25)")
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT count(x) FROM schema_rf_x.tab_rf_x") == 7, (
        "check table tab_rf_x should not be filtered"
    )
    assert subscriber.sql("SELECT * FROM public.tab_rf_partition ORDER BY 1") == [
        20,
        25,
    ], "check table tab_rf_partition should be filtered"

    publisher.sql("DROP PUBLICATION tap_pub_allinschema")
    publisher.sql("DROP PUBLICATION tap_pub_x")
    publisher.sql("DROP TABLE public.tab_rf_partition")
    publisher.sql("DROP TABLE schema_rf_x.tab_rf_partitioned")
    publisher.sql("DROP TABLE schema_rf_x.tab_rf_x")
    publisher.sql("DROP SCHEMA schema_rf_x")
    subscriber.sql("DROP SUBSCRIPTION tap_sub")
    subscriber.sql("DROP TABLE public.tab_rf_partition")
    subscriber.sql("DROP TABLE schema_rf_x.tab_rf_partitioned")
    subscriber.sql("DROP TABLE schema_rf_x.tab_rf_x")
    subscriber.sql("DROP SCHEMA schema_rf_x")

    # ================================================================
    # FOR TABLE with row filter publications.
    publisher.sql_batch(
        "CREATE TABLE tab_rowfilter_1 (a int primary key, b text)",
        "ALTER TABLE tab_rowfilter_1 REPLICA IDENTITY FULL",
        "CREATE TABLE tab_rowfilter_2 (c int primary key)",
        "CREATE TABLE tab_rowfilter_3 (a int primary key, b boolean)",
        "CREATE TABLE tab_rowfilter_4 (c int primary key)",
        "CREATE TABLE tab_rowfilter_partitioned (a int primary key, b integer) PARTITION BY RANGE(a)",
        "CREATE TABLE tab_rowfilter_less_10k (LIKE tab_rowfilter_partitioned)",
        "ALTER TABLE tab_rowfilter_partitioned ATTACH PARTITION tab_rowfilter_less_10k FOR VALUES FROM (MINVALUE) TO (10000)",
        "CREATE TABLE tab_rowfilter_greater_10k (LIKE tab_rowfilter_partitioned)",
        "ALTER TABLE tab_rowfilter_partitioned ATTACH PARTITION tab_rowfilter_greater_10k FOR VALUES FROM (10000) TO (MAXVALUE)",
        "CREATE TABLE tab_rowfilter_partitioned_2 (a int primary key, b integer) PARTITION BY RANGE(a)",
        "CREATE TABLE tab_rowfilter_partition (LIKE tab_rowfilter_partitioned_2)",
        "ALTER TABLE tab_rowfilter_partitioned_2 ATTACH PARTITION tab_rowfilter_partition DEFAULT",
        "CREATE TABLE tab_rowfilter_toast (a text NOT NULL, b text NOT NULL)",
        "ALTER TABLE tab_rowfilter_toast ALTER COLUMN a SET STORAGE EXTERNAL",
        "CREATE UNIQUE INDEX tab_rowfilter_toast_ri_index on tab_rowfilter_toast (a, b)",
        "ALTER TABLE tab_rowfilter_toast REPLICA IDENTITY USING INDEX tab_rowfilter_toast_ri_index",
        "CREATE TABLE tab_rowfilter_inherited (a int)",
        "CREATE TABLE tab_rowfilter_child (b text) INHERITS (tab_rowfilter_inherited)",
        "CREATE TABLE tab_rowfilter_viaroot_part (a int) PARTITION BY RANGE (a)",
        "CREATE TABLE tab_rowfilter_viaroot_part_1 PARTITION OF tab_rowfilter_viaroot_part FOR VALUES FROM (1) TO (20)",
        "CREATE TABLE tab_rowfilter_parent_sync (a int) PARTITION BY RANGE (a)",
        "CREATE TABLE tab_rowfilter_child_sync PARTITION OF tab_rowfilter_parent_sync FOR VALUES FROM (1) TO (20)",
        "CREATE TABLE tab_rowfilter_virtual (id int PRIMARY KEY, x int, y int GENERATED ALWAYS AS (x * 2) VIRTUAL)",
    )
    subscriber.sql_batch(
        "CREATE TABLE tab_rowfilter_1 (a int primary key, b text)",
        "CREATE TABLE tab_rowfilter_2 (c int primary key)",
        "CREATE TABLE tab_rowfilter_3 (a int primary key, b boolean)",
        "CREATE TABLE tab_rowfilter_4 (c int primary key)",
        "CREATE TABLE tab_rowfilter_partitioned (a int primary key, b integer) PARTITION BY RANGE(a)",
        "CREATE TABLE tab_rowfilter_less_10k (LIKE tab_rowfilter_partitioned)",
        "ALTER TABLE tab_rowfilter_partitioned ATTACH PARTITION tab_rowfilter_less_10k FOR VALUES FROM (MINVALUE) TO (10000)",
        "CREATE TABLE tab_rowfilter_greater_10k (LIKE tab_rowfilter_partitioned)",
        "ALTER TABLE tab_rowfilter_partitioned ATTACH PARTITION tab_rowfilter_greater_10k FOR VALUES FROM (10000) TO (MAXVALUE)",
        "CREATE TABLE tab_rowfilter_partitioned_2 (a int primary key, b integer) PARTITION BY RANGE(a)",
        "CREATE TABLE tab_rowfilter_partition (LIKE tab_rowfilter_partitioned_2)",
        "ALTER TABLE tab_rowfilter_partitioned_2 ATTACH PARTITION tab_rowfilter_partition DEFAULT",
        "CREATE TABLE tab_rowfilter_toast (a text NOT NULL, b text NOT NULL)",
        "CREATE UNIQUE INDEX tab_rowfilter_toast_ri_index on tab_rowfilter_toast (a, b)",
        "ALTER TABLE tab_rowfilter_toast REPLICA IDENTITY USING INDEX tab_rowfilter_toast_ri_index",
        "CREATE TABLE tab_rowfilter_inherited (a int)",
        "CREATE TABLE tab_rowfilter_child (b text) INHERITS (tab_rowfilter_inherited)",
        "CREATE TABLE tab_rowfilter_viaroot_part (a int)",
        "CREATE TABLE tab_rowfilter_viaroot_part_1 (a int)",
        "CREATE TABLE tab_rowfilter_parent_sync (a int)",
        "CREATE TABLE tab_rowfilter_child_sync (a int)",
        "CREATE TABLE tab_rowfilter_virtual (id int PRIMARY KEY, x int, y int GENERATED ALWAYS AS (x * 2) VIRTUAL)",
    )

    # Set up the publications.
    publisher.sql(
        "CREATE PUBLICATION tap_pub_1 FOR TABLE tab_rowfilter_1 "
        "WHERE (a > 1000 AND b <> 'filtered')"
    )
    publisher.sql(
        "ALTER PUBLICATION tap_pub_1 ADD TABLE tab_rowfilter_2 WHERE (c % 7 = 0)"
    )
    publisher.sql(
        "ALTER PUBLICATION tap_pub_1 SET TABLE "
        "tab_rowfilter_1 WHERE (a > 1000 AND b <> 'filtered'), "
        "tab_rowfilter_2 WHERE (c % 2 = 0), tab_rowfilter_3"
    )
    publisher.sql(
        "CREATE PUBLICATION tap_pub_2 FOR TABLE tab_rowfilter_2 WHERE (c % 3 = 0)"
    )
    publisher.sql("CREATE PUBLICATION tap_pub_3 FOR TABLE tab_rowfilter_partitioned")
    publisher.sql(
        "ALTER PUBLICATION tap_pub_3 ADD TABLE tab_rowfilter_less_10k WHERE (a < 6000)"
    )
    publisher.sql(
        "CREATE PUBLICATION tap_pub_not_used FOR TABLE tab_rowfilter_1 WHERE (a < 0)"
    )
    publisher.sql(
        "CREATE PUBLICATION tap_pub_4a FOR TABLE tab_rowfilter_4 WHERE (c % 2 = 0)"
    )
    publisher.sql("CREATE PUBLICATION tap_pub_4b FOR TABLE tab_rowfilter_4")
    publisher.sql("CREATE PUBLICATION tap_pub_5a FOR TABLE tab_rowfilter_partitioned_2")
    publisher.sql(
        "CREATE PUBLICATION tap_pub_5b FOR TABLE tab_rowfilter_partition WHERE (a > 10)"
    )
    publisher.sql(
        "CREATE PUBLICATION tap_pub_toast FOR TABLE tab_rowfilter_toast "
        "WHERE (a = repeat('1234567890', 200) AND b < '10')"
    )
    publisher.sql(
        "CREATE PUBLICATION tap_pub_inherits FOR TABLE tab_rowfilter_inherited WHERE (a > 15)"
    )
    # Two publications, each publishing the partition through a different
    # ancestor, with different row filters.
    publisher.sql(
        "CREATE PUBLICATION tap_pub_viaroot_1 FOR TABLE tab_rowfilter_viaroot_part "
        "WHERE (a > 15) WITH (publish_via_partition_root)"
    )
    publisher.sql(
        "CREATE PUBLICATION tap_pub_viaroot_2 FOR TABLE tab_rowfilter_viaroot_part_1 "
        "WHERE (a < 15) WITH (publish_via_partition_root)"
    )
    # Two publications, one through the ancestor and another directly on the
    # partition, with different row filters.
    publisher.sql(
        "CREATE PUBLICATION tap_pub_parent_sync FOR TABLE tab_rowfilter_parent_sync "
        "WHERE (a > 15) WITH (publish_via_partition_root)"
    )
    publisher.sql(
        "CREATE PUBLICATION tap_pub_child_sync FOR TABLE tab_rowfilter_child_sync WHERE (a < 15)"
    )
    # Publication using a virtual generated column in the row filter.
    publisher.sql(
        "CREATE PUBLICATION tap_pub_virtual FOR TABLE tab_rowfilter_virtual WHERE (y > 10)"
    )

    # These INSERTs run before CREATE SUBSCRIPTION, so they test the initial
    # data copy.
    publisher.sql_batch(
        "INSERT INTO tab_rowfilter_1 (a, b) VALUES (1, 'not replicated')",
        "INSERT INTO tab_rowfilter_1 (a, b) VALUES (1500, 'filtered')",
        "INSERT INTO tab_rowfilter_1 (a, b) VALUES (1980, 'not filtered')",
        "INSERT INTO tab_rowfilter_1 (a, b) SELECT x, 'test ' || x FROM generate_series(990,1002) x",
        "INSERT INTO tab_rowfilter_2 (c) SELECT generate_series(1, 20)",
        "INSERT INTO tab_rowfilter_3 (a, b) SELECT x, (x % 3 = 0) FROM generate_series(1, 10) x",
        "INSERT INTO tab_rowfilter_4 (c) SELECT generate_series(1, 10)",
        "INSERT INTO tab_rowfilter_parent_sync(a) VALUES(14), (16)",
        "INSERT INTO tab_rowfilter_partitioned (a, b) VALUES(1, 100),(7000, 101),(15000, 102),(5500, 300)",
        "INSERT INTO tab_rowfilter_less_10k (a, b) VALUES(2, 200),(6005, 201)",
        "INSERT INTO tab_rowfilter_greater_10k (a, b) VALUES(16000, 103)",
        "INSERT INTO tab_rowfilter_partitioned_2 (a, b) VALUES(1, 1),(20, 20)",
        "INSERT INTO tab_rowfilter_toast(a, b) VALUES(repeat('1234567890', 200), '1234567890')",
        "INSERT INTO tab_rowfilter_inherited(a) VALUES(10),(20)",
        "INSERT INTO tab_rowfilter_child(a, b) VALUES(0,'0'),(30,'30'),(40,'40')",
        "INSERT INTO tab_rowfilter_virtual (id, x) VALUES (1, 2), (2, 4), (3, 6)",
    )

    subscriber.sql(
        f"CREATE SUBSCRIPTION tap_sub CONNECTION '{connstr}' PUBLICATION "
        "tap_pub_1, tap_pub_2, tap_pub_3, tap_pub_4a, tap_pub_4b, tap_pub_5a, "
        "tap_pub_5b, tap_pub_toast, tap_pub_inherits, tap_pub_viaroot_2, "
        "tap_pub_viaroot_1, tap_pub_parent_sync, tap_pub_child_sync, tap_pub_virtual"
    )
    subscriber.wait_for_subscription_sync(publisher, "tap_sub")

    # tab_rowfilter_1: filter (a > 1000 AND b <> 'filtered').
    assert subscriber.sql("SELECT a, b FROM tab_rowfilter_1 ORDER BY 1, 2") == [
        (1001, "test 1001"),
        (1002, "test 1002"),
        (1980, "not filtered"),
    ], "check initial data copy from table tab_rowfilter_1"

    # tab_rowfilter_2: filters (c % 2 = 0) and (c % 3 = 0) OR'ed together.
    assert subscriber.sql("SELECT count(c), min(c), max(c) FROM tab_rowfilter_2") == (
        13,
        2,
        20,
    ), "check initial data copy from table tab_rowfilter_2"

    # tab_rowfilter_4: same table in two pubs, only one filtered → no filter.
    assert subscriber.sql("SELECT count(c), min(c), max(c) FROM tab_rowfilter_4") == (
        10,
        1,
        10,
    ), "check initial data copy from table tab_rowfilter_4"

    # tab_rowfilter_3: no filter, 10 rows.
    assert subscriber.sql("SELECT count(a) FROM tab_rowfilter_3") == 10, (
        "check initial data copy from table tab_rowfilter_3"
    )

    # Partitions with publish_via_partition_root = false use the partition's filter.
    assert subscriber.sql("SELECT a, b FROM tab_rowfilter_less_10k ORDER BY 1, 2") == [
        (1, 100),
        (2, 200),
        (5500, 300),
    ], "check initial data copy from partition tab_rowfilter_less_10k"
    assert subscriber.sql(
        "SELECT a, b FROM tab_rowfilter_greater_10k ORDER BY 1, 2"
    ) == [
        (15000, 102),
        (16000, 103),
    ], "check initial data copy from partition tab_rowfilter_greater_10k"

    # tap_pub_5a has no filter on the parent, so the partition is unfiltered.
    assert subscriber.sql("SELECT a, b FROM tab_rowfilter_partition ORDER BY 1, 2") == [
        (1, 1),
        (20, 20),
    ], "check initial data copy from partition tab_rowfilter_partition"

    # tab_rowfilter_toast: filter (a = repeat('1234567890', 200) AND b < '10').
    assert subscriber.sql("SELECT count(*) FROM tab_rowfilter_toast") == 0, (
        "check initial data copy from table tab_rowfilter_toast"
    )

    # tab_rowfilter_inherited: filter (a > 15).
    assert subscriber.sql("SELECT a FROM tab_rowfilter_inherited ORDER BY a") == [
        20,
        30,
        40,
    ], "check initial data copy from table tab_rowfilter_inherited"

    # tap_pub_parent_sync (publish_via_partition_root) filter (a > 15) wins.
    assert subscriber.sql("SELECT a FROM tab_rowfilter_parent_sync ORDER BY 1") == 16, (
        "check initial data copy from tab_rowfilter_parent_sync"
    )
    assert subscriber.sql("SELECT a FROM tab_rowfilter_child_sync ORDER BY 1") == [], (
        "check initial data copy from tab_rowfilter_child_sync"
    )

    # tab_rowfilter_virtual: filter (y > 10), y generated as (x * 2).
    assert subscriber.sql("SELECT id, x FROM tab_rowfilter_virtual ORDER BY id") == (
        3,
        6,
    ), "check initial data copy from table tab_rowfilter_virtual"

    # The following run after CREATE SUBSCRIPTION: normal replication behavior.
    publisher.sql_batch(
        "INSERT INTO tab_rowfilter_1 (a, b) VALUES (800, 'test 800')",
        "INSERT INTO tab_rowfilter_1 (a, b) VALUES (1600, 'test 1600')",
        "INSERT INTO tab_rowfilter_1 (a, b) VALUES (1601, 'test 1601')",
        "INSERT INTO tab_rowfilter_1 (a, b) VALUES (1602, 'filtered')",
        "INSERT INTO tab_rowfilter_1 (a, b) VALUES (1700, 'test 1700')",
        "UPDATE tab_rowfilter_1 SET b = NULL WHERE a = 1600",
        "UPDATE tab_rowfilter_1 SET b = 'test 1601 updated' WHERE a = 1601",
        "UPDATE tab_rowfilter_1 SET b = 'test 1602 updated' WHERE a = 1602",
        "DELETE FROM tab_rowfilter_1 WHERE a = 1700",
        "INSERT INTO tab_rowfilter_2 (c) VALUES (21), (22), (23), (24), (25)",
        "INSERT INTO tab_rowfilter_4 (c) VALUES (0), (11), (12)",
        "INSERT INTO tab_rowfilter_inherited (a) VALUES (14), (16)",
        "INSERT INTO tab_rowfilter_child (a, b) VALUES (13, '13'), (17, '17')",
        "INSERT INTO tab_rowfilter_viaroot_part (a) VALUES (14), (15), (16)",
        "INSERT INTO tab_rowfilter_virtual (id, x) VALUES (4, 3), (5, 7)",
    )
    publisher.wait_for_catchup("tap_sub")

    # tab_rowfilter_2: original (2,3,4,6,8,9,10,12,14,15,16,18,20) plus (21,22,24).
    assert subscriber.sql("SELECT count(c), min(c), max(c) FROM tab_rowfilter_2") == (
        16,
        2,
        24,
    ), "check replicated rows to tab_rowfilter_2"

    # tab_rowfilter_4: all initial rows plus (0, 11, 12).
    assert subscriber.sql("SELECT count(c), min(c), max(c) FROM tab_rowfilter_4") == (
        13,
        0,
        12,
    ), "check replicated rows to tab_rowfilter_4"

    # tab_rowfilter_1: filter (a > 1000 AND b <> 'filtered').
    assert subscriber.sql("SELECT a, b FROM tab_rowfilter_1 ORDER BY 1, 2") == [
        (1001, "test 1001"),
        (1002, "test 1002"),
        (1601, "test 1601 updated"),
        (1602, "test 1602 updated"),
        (1980, "not filtered"),
    ], "check replicated rows to table tab_rowfilter_1"

    # Publish using the root partitioned table (exercise publish_via_partition_root).
    publisher.sql("ALTER PUBLICATION tap_pub_3 SET (publish_via_partition_root = true)")
    publisher.sql(
        "ALTER PUBLICATION tap_pub_3 SET TABLE tab_rowfilter_partitioned WHERE (a < 5000), "
        "tab_rowfilter_less_10k WHERE (a < 6000)"
    )
    subscriber.sql("TRUNCATE TABLE tab_rowfilter_partitioned")
    subscriber.sql(
        "ALTER SUBSCRIPTION tap_sub REFRESH PUBLICATION WITH (copy_data = true)"
    )
    subscriber.wait_for_subscription_sync()

    publisher.sql_batch(
        "INSERT INTO tab_rowfilter_partitioned (a, b) VALUES(4000, 400),(4001, 401),(4002, 402)",
        "INSERT INTO tab_rowfilter_less_10k (a, b) VALUES(4500, 450)",
        "INSERT INTO tab_rowfilter_less_10k (a, b) VALUES(5600, 123)",
        "INSERT INTO tab_rowfilter_greater_10k (a, b) VALUES(14000, 1950)",
        "UPDATE tab_rowfilter_less_10k SET b = 30 WHERE a = 4001",
        "DELETE FROM tab_rowfilter_less_10k WHERE a = 4002",
    )
    publisher.wait_for_catchup("tap_sub")

    # publish_via_partition_root = true so the root's filter (a < 5000) applies.
    assert subscriber.sql(
        "SELECT a, b FROM tab_rowfilter_partitioned ORDER BY 1, 2"
    ) == [
        (1, 100),
        (2, 200),
        (4000, 400),
        (4001, 30),
        (4500, 450),
    ], "check publish_via_partition_root behavior"

    # tab_rowfilter_inherited / tab_rowfilter_child: filter (a > 15).
    assert subscriber.sql("SELECT a FROM tab_rowfilter_inherited ORDER BY a") == [
        16,
        17,
        20,
        30,
        40,
    ], "check replicated rows to tab_rowfilter_inherited and tab_rowfilter_child"

    # tab_rowfilter_virtual: filter (y > 10), y generated as (x * 2).
    assert subscriber.sql("SELECT id, x FROM tab_rowfilter_virtual ORDER BY id") == [
        (3, 6),
        (5, 7),
    ], "check replicated rows to tab_rowfilter_virtual"

    # UPDATE the non-toasted column for tab_rowfilter_toast.
    publisher.sql("UPDATE tab_rowfilter_toast SET b = '1'")
    publisher.wait_for_catchup("tap_sub")

    # filter (a = repeat('1234567890', 200) AND b < '10'): new tuple matches.
    assert subscriber.sql(
        "SELECT a = repeat('1234567890', 200), b FROM tab_rowfilter_toast"
    ) == (True, "1"), "check replicated rows to tab_rowfilter_toast"

    # tab_rowfilter_viaroot_part: only rows matching the top-level filter (a > 15).
    assert subscriber.sql("SELECT a FROM tab_rowfilter_viaroot_part") == 16, (
        "check replicated rows to tab_rowfilter_viaroot_part"
    )
    # Rows go via the topmost parent, so the partition itself is empty.
    assert subscriber.sql("SELECT a FROM tab_rowfilter_viaroot_part_1") == [], (
        "check replicated rows to tab_rowfilter_viaroot_part_1"
    )
