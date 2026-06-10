# Copyright (c) 2022-2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/031_column_list.pl.

Tests partial-column publication of tables: column lists on FOR TABLE
publications control which columns are copied during initial sync and
replicated for INSERT/UPDATE/DELETE, including weird quoted column names,
partitioned tables with publish_via_partition_root, replica-identity changes,
generated/dropped columns, FOR ALL TABLES / TABLES IN SCHEMA overriding the
list, and the errors raised when a table appears in two publications with
different column lists.
"""

from libpq import LibpqError

import pytest


def test_column_list(create_pg):
    publisher = create_pg("publisher", allows_streaming="logical")
    subscriber = create_pg("subscriber", conf={"max_logical_replication_workers": 6})
    connstr = publisher.connstr()

    # --- set up tables on both nodes -----------------------------------------
    # tab1: simple 1:1 replication
    publisher.sql('CREATE TABLE tab1 (a int PRIMARY KEY, "B" int, c int)')
    subscriber.sql('CREATE TABLE tab1 (a int PRIMARY KEY, "B" int, c int)')

    # tab2: regular table to a table with fewer columns
    publisher.sql("CREATE TABLE tab2 (a int PRIMARY KEY, b varchar, c int)")
    subscriber.sql("CREATE TABLE tab2 (a int PRIMARY KEY, b varchar)")

    # tab3: simple 1:1 replication with weird column names
    publisher.sql('CREATE TABLE tab3 ("a\'" int PRIMARY KEY, "B" varchar, "c\'" int)')
    subscriber.sql('CREATE TABLE tab3 ("a\'" int PRIMARY KEY, "c\'" int)')

    # test_part: partitioned (incl. multi-level), fewer columns on subscriber
    publisher.sql_batch(
        "CREATE TABLE test_part (a int PRIMARY KEY, b text, c timestamptz) PARTITION BY LIST (a)",
        "CREATE TABLE test_part_1_1 PARTITION OF test_part FOR VALUES IN (1,2,3,4,5,6)",
        "CREATE TABLE test_part_2_1 PARTITION OF test_part FOR VALUES IN (7,8,9,10,11,12) PARTITION BY LIST (a)",
        "CREATE TABLE test_part_2_2 PARTITION OF test_part_2_1 FOR VALUES IN (7,8,9,10)",
    )
    subscriber.sql_batch(
        "CREATE TABLE test_part (a int PRIMARY KEY, b text) PARTITION BY LIST (a)",
        "CREATE TABLE test_part_1_1 PARTITION OF test_part FOR VALUES IN (1,2,3,4,5,6)",
        "CREATE TABLE test_part_2_1 PARTITION OF test_part FOR VALUES IN (7,8,9,10,11,12) PARTITION BY LIST (a)",
        "CREATE TABLE test_part_2_2 PARTITION OF test_part_2_1 FOR VALUES IN (7,8,9,10)",
    )

    # tab4: user-defined enum type
    publisher.sql_batch(
        "CREATE TYPE test_typ AS ENUM ('blue', 'red')",
        "CREATE TABLE tab4 (a INT PRIMARY KEY, b test_typ, c int, d text)",
    )
    subscriber.sql_batch(
        "CREATE TYPE test_typ AS ENUM ('blue', 'red')",
        "CREATE TABLE tab4 (a INT PRIMARY KEY, b test_typ, d text)",
    )

    # --- create publication with column lists --------------------------------
    publisher.sql(
        """
        CREATE PUBLICATION pub1
           FOR TABLE tab1 (a, "B"), tab3 ("a'", "c'"), test_part (a, b), tab4 (a, b, d)
          WITH (publish_via_partition_root = 'true');
        """
    )

    # Check the prattrs values landed in pg_publication_rel (stable by relname).
    assert publisher.sql(
        """
        SELECT relname, prattrs
        FROM pg_publication_rel pb JOIN pg_class pc ON(pb.prrelid = pc.oid)
        ORDER BY relname
        """
    ) == [
        ("tab1", "1 2"),
        ("tab3", "1 3"),
        ("tab4", "1 2 4"),
        ("test_part", "1 2"),
    ], "publication relation updated"

    # --- insert data, then create subscription and check the sync ------------
    publisher.sql_batch(
        "INSERT INTO tab1 VALUES (1, 2, 3)", "INSERT INTO tab1 VALUES (4, 5, 6)"
    )
    publisher.sql_batch(
        "INSERT INTO tab3 VALUES (1, 2, 3)", "INSERT INTO tab3 VALUES (4, 5, 6)"
    )
    publisher.sql_batch(
        "INSERT INTO tab4 VALUES (1, 'red', 3, 'oh my')",
        "INSERT INTO tab4 VALUES (2, 'blue', 4, 'hello')",
    )
    publisher.sql_batch(
        "INSERT INTO test_part VALUES (1, 'abc', '2021-07-04 12:00:00')",
        "INSERT INTO test_part VALUES (2, 'bcd', '2021-07-03 11:12:13')",
        "INSERT INTO test_part VALUES (7, 'abc', '2021-07-04 12:00:00')",
        "INSERT INTO test_part VALUES (8, 'bcd', '2021-07-03 11:12:13')",
    )

    subscriber.sql(f"CREATE SUBSCRIPTION sub1 CONNECTION '{connstr}' PUBLICATION pub1")
    subscriber.wait_for_subscription_sync()

    # tab1: only (a, B) is replicated
    assert subscriber.sql("SELECT * FROM tab1 ORDER BY a") == [
        (1, 2, None),
        (4, 5, None),
    ], "insert on column tab1.c is not replicated"

    # tab3: only (a, c) is replicated
    assert subscriber.sql('SELECT * FROM tab3 ORDER BY "a\'"') == [
        (1, 3),
        (4, 6),
    ], "insert on column tab3.b is not replicated"

    # tab4: only (a, b, d) is replicated
    assert subscriber.sql("SELECT * FROM tab4 ORDER BY a") == [
        (1, "red", "oh my"),
        (2, "blue", "hello"),
    ], "insert on column tab4.c is not replicated"

    # test_part: (a, b) is replicated
    assert subscriber.sql("SELECT * FROM test_part ORDER BY a") == [
        (1, "abc"),
        (2, "bcd"),
        (7, "abc"),
        (8, "bcd"),
    ], "insert on column test_part.c columns is not replicated"

    # --- more inserts, replicated by regular decoding (not tablesync) --------
    publisher.sql_batch(
        "INSERT INTO tab1 VALUES (2, 3, 4)", "INSERT INTO tab1 VALUES (5, 6, 7)"
    )
    publisher.sql_batch(
        "INSERT INTO tab3 VALUES (2, 3, 4)", "INSERT INTO tab3 VALUES (5, 6, 7)"
    )
    publisher.sql_batch(
        "INSERT INTO tab4 VALUES (3, 'red', 5, 'foo')",
        "INSERT INTO tab4 VALUES (4, 'blue', 6, 'bar')",
    )
    publisher.sql_batch(
        "INSERT INTO test_part VALUES (3, 'xxx', '2022-02-01 10:00:00')",
        "INSERT INTO test_part VALUES (4, 'yyy', '2022-03-02 15:12:13')",
        "INSERT INTO test_part VALUES (9, 'zzz', '2022-04-03 21:00:00')",
        "INSERT INTO test_part VALUES (10, 'qqq', '2022-05-04 22:12:13')",
    )
    publisher.wait_for_catchup("sub1")

    assert subscriber.sql("SELECT * FROM tab1 ORDER BY a") == [
        (1, 2, None),
        (2, 3, None),
        (4, 5, None),
        (5, 6, None),
    ], "insert on column tab1.c is not replicated"
    assert subscriber.sql('SELECT * FROM tab3 ORDER BY "a\'"') == [
        (1, 3),
        (2, 4),
        (4, 6),
        (5, 7),
    ], "insert on column tab3.b is not replicated"
    assert subscriber.sql("SELECT * FROM tab4 ORDER BY a") == [
        (1, "red", "oh my"),
        (2, "blue", "hello"),
        (3, "red", "foo"),
        (4, "blue", "bar"),
    ], "insert on column tab4.c is not replicated"
    assert subscriber.sql("SELECT * FROM test_part ORDER BY a") == [
        (1, "abc"),
        (2, "bcd"),
        (3, "xxx"),
        (4, "yyy"),
        (7, "abc"),
        (8, "bcd"),
        (9, "zzz"),
        (10, "qqq"),
    ], "insert on column test_part.c columns is not replicated"

    # --- updates on replicated and non-replicated columns --------------------
    publisher.sql('UPDATE tab1 SET "B" = 2 * "B" where a = 1')
    publisher.sql("UPDATE tab1 SET c = 2*c where a = 4")
    publisher.sql('UPDATE tab3 SET "B" = "B" || \' updated\' where "a\'" = 4')
    publisher.sql('UPDATE tab3 SET "c\'" = 2 * "c\'" where "a\'" = 1')
    publisher.sql(
        "UPDATE tab4 SET b = 'blue', c = c * 2, d = d || ' updated' where a = 1"
    )
    publisher.sql(
        "UPDATE tab4 SET b = 'red', c = c * 2, d = d || ' updated' where a = 2"
    )
    publisher.wait_for_catchup("sub1")

    assert subscriber.sql("SELECT * FROM tab1 ORDER BY a") == [
        (1, 4, None),
        (2, 3, None),
        (4, 5, None),
        (5, 6, None),
    ], "only update on column tab1.b is replicated"
    assert subscriber.sql('SELECT * FROM tab3 ORDER BY "a\'"') == [
        (1, 6),
        (2, 4),
        (4, 6),
        (5, 7),
    ], "only update on column tab3.c is replicated"
    assert subscriber.sql("SELECT * FROM tab4 ORDER BY a") == [
        (1, "blue", "oh my updated"),
        (2, "red", "hello updated"),
        (3, "red", "foo"),
        (4, "blue", "bar"),
    ], "update on column tab4.c is not replicated"

    # --- add table with column list, insert, replicate -----------------------
    publisher.sql("INSERT INTO tab2 VALUES (1, 'abc', 3)")
    publisher.sql("ALTER PUBLICATION pub1 ADD TABLE tab2 (a, b)")
    subscriber.sql("ALTER SUBSCRIPTION sub1 REFRESH PUBLICATION")
    subscriber.wait_for_subscription_sync()

    publisher.sql("INSERT INTO tab2 VALUES (2, 'def', 6)")
    publisher.wait_for_catchup("sub1")
    assert subscriber.sql("SELECT * FROM tab2 ORDER BY a") == [
        (1, "abc"),
        (2, "def"),
    ], "insert on column tab2.c is not replicated"

    publisher.sql_batch(
        "UPDATE tab2 SET c = 5 where a = 1", "UPDATE tab2 SET b = 'xyz' where a = 2"
    )
    publisher.wait_for_catchup("sub1")
    assert subscriber.sql("SELECT * FROM tab2 ORDER BY a") == [
        (1, "abc"),
        (2, "xyz"),
    ], "update on column tab2.c is not replicated"

    # --- table in two publications with the same column lists ----------------
    publisher.sql_batch(
        "CREATE TABLE tab5 (a int PRIMARY KEY, b int, c int, d int)",
        "CREATE PUBLICATION pub2 FOR TABLE tab5 (a, b)",
        "CREATE PUBLICATION pub3 FOR TABLE tab5 (a, b)",
        "INSERT INTO tab5 VALUES (1, 11, 111, 1111)",
        "INSERT INTO tab5 VALUES (2, 22, 222, 2222)",
    )
    subscriber.sql("CREATE TABLE tab5 (a int PRIMARY KEY, b int, d int)")
    subscriber.sql("DROP SUBSCRIPTION sub1")
    subscriber.sql(
        f"CREATE SUBSCRIPTION sub1 CONNECTION '{connstr}' PUBLICATION pub2, pub3"
    )
    subscriber.wait_for_subscription_sync(publisher, "sub1")

    publisher.sql_batch(
        "INSERT INTO tab5 VALUES (3, 33, 333, 3333)",
        "INSERT INTO tab5 VALUES (4, 44, 444, 4444)",
    )
    publisher.wait_for_catchup("sub1")
    assert subscriber.sql("SELECT * FROM tab5 ORDER BY a") == [
        (1, 11, None),
        (2, 22, None),
        (3, 33, None),
        (4, 44, None),
    ], "overlapping publications with overlapping column lists"

    # --- change replica identity, moving PK within the column list -----------
    publisher.sql_batch(
        "CREATE TABLE tab6 (a int PRIMARY KEY, b int, c int, d int)",
        "CREATE PUBLICATION pub4 FOR TABLE tab6 (a, b)",
        "INSERT INTO tab6 VALUES (1, 22, 333, 4444)",
    )
    subscriber.sql("CREATE TABLE tab6 (a int PRIMARY KEY, b int, c int, d int)")
    subscriber.sql("DROP SUBSCRIPTION sub1")
    subscriber.sql(f"CREATE SUBSCRIPTION sub1 CONNECTION '{connstr}' PUBLICATION pub4")
    subscriber.wait_for_subscription_sync()

    publisher.sql_batch(
        "INSERT INTO tab6 VALUES (2, 33, 444, 5555)",
        "UPDATE tab6 SET b = b * 2, c = c * 3, d = d * 4",
    )
    publisher.wait_for_catchup("sub1")
    assert subscriber.sql("SELECT * FROM tab6 ORDER BY a") == [
        (1, 44, None, None),
        (2, 66, None, None),
    ], "replication with the original primary key"

    # Move the PK to a different column (still covered by the column list).
    publisher.sql_batch(
        "ALTER TABLE tab6 DROP CONSTRAINT tab6_pkey",
        "ALTER TABLE tab6 ADD PRIMARY KEY (b)",
    )
    subscriber.sql_batch(
        "ALTER TABLE tab6 DROP CONSTRAINT tab6_pkey",
        "ALTER TABLE tab6 ADD PRIMARY KEY (b)",
    )
    subscriber.sql("ALTER SUBSCRIPTION sub1 REFRESH PUBLICATION")
    subscriber.wait_for_subscription_sync()

    publisher.sql_batch(
        "INSERT INTO tab6 VALUES (3, 55, 666, 8888)",
        "UPDATE tab6 SET b = b * 2, c = c * 3, d = d * 4",
    )
    publisher.wait_for_catchup("sub1")
    assert subscriber.sql("SELECT * FROM tab6 ORDER BY a") == [
        (1, 88, None, None),
        (2, 132, None, None),
        (3, 110, None, None),
    ], "replication with the modified primary key"

    # --- change RI to a multi-column key, all covered by the column list -----
    publisher.sql_batch(
        "CREATE TABLE tab7 (a int PRIMARY KEY, b int, c int, d int)",
        "CREATE PUBLICATION pub5 FOR TABLE tab7 (a, b)",
        "INSERT INTO tab7 VALUES (1, 22, 333, 4444)",
    )
    subscriber.sql("CREATE TABLE tab7 (a int PRIMARY KEY, b int, c int, d int)")
    subscriber.sql("DROP SUBSCRIPTION sub1")
    subscriber.sql(f"CREATE SUBSCRIPTION sub1 CONNECTION '{connstr}' PUBLICATION pub5")
    subscriber.wait_for_subscription_sync()

    publisher.sql_batch(
        "INSERT INTO tab7 VALUES (2, 33, 444, 5555)",
        "UPDATE tab7 SET b = b * 2, c = c * 3, d = d * 4",
    )
    publisher.wait_for_catchup("sub1")
    assert subscriber.sql("SELECT * FROM tab7 ORDER BY a") == [
        (1, 44, None, None),
        (2, 66, None, None),
    ], "replication with the original primary key"

    publisher.sql_batch(
        "ALTER TABLE tab7 DROP CONSTRAINT tab7_pkey",
        "ALTER TABLE tab7 ADD PRIMARY KEY (a, b)",
    )
    publisher.sql_batch(
        "INSERT INTO tab7 VALUES (3, 55, 666, 7777)",
        "UPDATE tab7 SET b = b * 2, c = c * 3, d = d * 4",
    )
    publisher.wait_for_catchup("sub1")
    assert subscriber.sql("SELECT * FROM tab7 ORDER BY a") == [
        (1, 88, None, None),
        (2, 132, None, None),
        (3, 110, None, None),
    ], "replication with the modified primary key"

    # Switch the PK again, with writes between the drop and re-create.
    publisher.sql_batch(
        "ALTER TABLE tab7 DROP CONSTRAINT tab7_pkey",
        "INSERT INTO tab7 VALUES (4, 77, 888, 9999)",
        "ALTER TABLE tab7 ADD PRIMARY KEY (b, a)",
        "UPDATE tab7 SET b = b * 2, c = c * 3, d = d * 4",
        "DELETE FROM tab7 WHERE a = 1",
    )
    publisher.wait_for_catchup("sub1")
    assert subscriber.sql("SELECT * FROM tab7 ORDER BY a") == [
        (2, 264, None, None),
        (3, 220, None, None),
        (4, 154, None, None),
    ], "replication with the modified primary key"

    # --- partitioned tables with different leaf RI (pub_via_root=false) ------
    publisher.sql_batch(
        "CREATE TABLE test_part_a (a int, b int, c int) PARTITION BY LIST (a)",
        "CREATE TABLE test_part_a_1 PARTITION OF test_part_a FOR VALUES IN (1,2,3,4,5)",
        "ALTER TABLE test_part_a_1 ADD PRIMARY KEY (a)",
        "ALTER TABLE test_part_a_1 REPLICA IDENTITY USING INDEX test_part_a_1_pkey",
        "CREATE TABLE test_part_a_2 PARTITION OF test_part_a FOR VALUES IN (6,7,8,9,10)",
        "ALTER TABLE test_part_a_2 ADD PRIMARY KEY (b)",
        "ALTER TABLE test_part_a_2 REPLICA IDENTITY USING INDEX test_part_a_2_pkey",
        "INSERT INTO test_part_a VALUES (1, 3)",
        "INSERT INTO test_part_a VALUES (6, 4)",
    )
    # Same on the subscriber, but with the opposite column order.
    subscriber.sql_batch(
        "CREATE TABLE test_part_a (b int, a int) PARTITION BY LIST (a)",
        "CREATE TABLE test_part_a_1 PARTITION OF test_part_a FOR VALUES IN (1,2,3,4,5)",
        "ALTER TABLE test_part_a_1 ADD PRIMARY KEY (a)",
        "ALTER TABLE test_part_a_1 REPLICA IDENTITY USING INDEX test_part_a_1_pkey",
        "CREATE TABLE test_part_a_2 PARTITION OF test_part_a FOR VALUES IN (6,7,8,9,10)",
        "ALTER TABLE test_part_a_2 ADD PRIMARY KEY (b)",
        "ALTER TABLE test_part_a_2 REPLICA IDENTITY USING INDEX test_part_a_2_pkey",
    )
    publisher.sql_batch(
        "CREATE PUBLICATION pub6 FOR TABLE test_part_a (b, a) WITH (publish_via_partition_root = true)",
        "ALTER PUBLICATION pub6 ADD TABLE test_part_a_1 (a)",
        "ALTER PUBLICATION pub6 ADD TABLE test_part_a_2 (b)",
    )
    subscriber.sql("DROP SUBSCRIPTION sub1")
    subscriber.sql(f"CREATE SUBSCRIPTION sub1 CONNECTION '{connstr}' PUBLICATION pub6")
    subscriber.wait_for_subscription_sync()

    publisher.sql_batch(
        "INSERT INTO test_part_a VALUES (2, 5)", "INSERT INTO test_part_a VALUES (7, 6)"
    )
    publisher.wait_for_catchup("sub1")
    assert subscriber.sql("SELECT a, b FROM test_part_a ORDER BY a, b") == [
        (1, 3),
        (2, 5),
        (6, 4),
        (7, 6),
    ], "partitions with different replica identities not replicated correctly"

    # --- column list initially covers RI for all partitions ------------------
    publisher.sql_batch(
        "CREATE TABLE test_part_b (a int, b int) PARTITION BY LIST (a)",
        "CREATE TABLE test_part_b_1 PARTITION OF test_part_b FOR VALUES IN (1,2,3,4,5)",
        "ALTER TABLE test_part_b_1 ADD PRIMARY KEY (a)",
        "ALTER TABLE test_part_b_1 REPLICA IDENTITY USING INDEX test_part_b_1_pkey",
        "CREATE TABLE test_part_b_2 PARTITION OF test_part_b FOR VALUES IN (6,7,8,9,10)",
        "ALTER TABLE test_part_b_2 ADD PRIMARY KEY (b)",
        "ALTER TABLE test_part_b_2 REPLICA IDENTITY USING INDEX test_part_b_2_pkey",
        "INSERT INTO test_part_b VALUES (1, 1)",
        "INSERT INTO test_part_b VALUES (6, 2)",
    )
    subscriber.sql_batch(
        "CREATE TABLE test_part_b (a int, b int) PARTITION BY LIST (a)",
        "CREATE TABLE test_part_b_1 PARTITION OF test_part_b FOR VALUES IN (1,2,3,4,5)",
        "ALTER TABLE test_part_b_1 ADD PRIMARY KEY (a)",
        "ALTER TABLE test_part_b_1 REPLICA IDENTITY USING INDEX test_part_b_1_pkey",
        "CREATE TABLE test_part_b_2 PARTITION OF test_part_b FOR VALUES IN (6,7,8,9,10)",
        "ALTER TABLE test_part_b_2 ADD PRIMARY KEY (b)",
        "ALTER TABLE test_part_b_2 REPLICA IDENTITY USING INDEX test_part_b_2_pkey",
    )
    publisher.sql(
        "CREATE PUBLICATION pub7 FOR TABLE test_part_b (a, b) WITH (publish_via_partition_root = true)"
    )
    subscriber.sql("DROP SUBSCRIPTION sub1")
    subscriber.sql(f"CREATE SUBSCRIPTION sub1 CONNECTION '{connstr}' PUBLICATION pub7")
    subscriber.wait_for_subscription_sync()

    publisher.sql_batch(
        "INSERT INTO test_part_b VALUES (2, 3)", "INSERT INTO test_part_b VALUES (7, 4)"
    )
    publisher.wait_for_catchup("sub1")
    assert subscriber.sql("SELECT * FROM test_part_b ORDER BY a, b") == [
        (1, 1),
        (2, 3),
        (6, 2),
        (7, 4),
    ], "partitions with different replica identities not replicated correctly"

    # --- pub_via_root=false, column lists on partitions are not applied ------
    publisher.sql_batch(
        "CREATE TABLE test_part_c (a int, b int, c int) PARTITION BY LIST (a)",
        "CREATE TABLE test_part_c_1 PARTITION OF test_part_c FOR VALUES IN (1,3)",
        "ALTER TABLE test_part_c_1 ADD PRIMARY KEY (a)",
        "ALTER TABLE test_part_c_1 REPLICA IDENTITY USING INDEX test_part_c_1_pkey",
        "CREATE TABLE test_part_c_2 PARTITION OF test_part_c FOR VALUES IN (2,4)",
        "ALTER TABLE test_part_c_2 ADD PRIMARY KEY (b)",
        "ALTER TABLE test_part_c_2 REPLICA IDENTITY USING INDEX test_part_c_2_pkey",
        "INSERT INTO test_part_c VALUES (1, 3, 5)",
        "INSERT INTO test_part_c VALUES (2, 4, 6)",
    )
    subscriber.sql_batch(
        "CREATE TABLE test_part_c (a int, b int, c int) PARTITION BY LIST (a)",
        "CREATE TABLE test_part_c_1 PARTITION OF test_part_c FOR VALUES IN (1,3)",
        "ALTER TABLE test_part_c_1 ADD PRIMARY KEY (a)",
        "ALTER TABLE test_part_c_1 REPLICA IDENTITY USING INDEX test_part_c_1_pkey",
        "CREATE TABLE test_part_c_2 PARTITION OF test_part_c FOR VALUES IN (2,4)",
        "ALTER TABLE test_part_c_2 ADD PRIMARY KEY (b)",
        "ALTER TABLE test_part_c_2 REPLICA IDENTITY USING INDEX test_part_c_2_pkey",
    )
    publisher.sql_batch(
        "CREATE PUBLICATION pub8 FOR TABLE test_part_c WITH (publish_via_partition_root = false)",
        "ALTER PUBLICATION pub8 ADD TABLE test_part_c_1 (a,c)",
        "ALTER PUBLICATION pub8 ADD TABLE test_part_c_2 (a,b)",
    )
    subscriber.sql("DROP SUBSCRIPTION sub1")
    subscriber.sql(f"CREATE SUBSCRIPTION sub1 CONNECTION '{connstr}' PUBLICATION pub8")
    subscriber.wait_for_subscription_sync()

    publisher.sql_batch(
        "INSERT INTO test_part_c VALUES (3, 7, 8)",
        "INSERT INTO test_part_c VALUES (4, 9, 10)",
    )
    publisher.wait_for_catchup("sub1")
    assert subscriber.sql("SELECT * FROM test_part_c ORDER BY a, b") == [
        (1, None, 5),
        (2, 4, None),
        (3, None, 8),
        (4, 9, None),
    ], "partitions with different replica identities not replicated correctly"

    # Recreate pub8 without a root column list; per-partition lists apply.
    publisher.sql_batch(
        "DROP PUBLICATION pub8",
        "CREATE PUBLICATION pub8 FOR TABLE test_part_c WITH (publish_via_partition_root = false)",
        "ALTER PUBLICATION pub8 ADD TABLE test_part_c_1 (a)",
        "ALTER PUBLICATION pub8 ADD TABLE test_part_c_2 (a,b)",
    )
    subscriber.sql("ALTER SUBSCRIPTION sub1 REFRESH PUBLICATION")
    subscriber.sql("TRUNCATE test_part_c")
    subscriber.wait_for_subscription_sync()

    publisher.sql("TRUNCATE test_part_c")
    publisher.sql_batch(
        "INSERT INTO test_part_c VALUES (1, 3, 5)",
        "INSERT INTO test_part_c VALUES (2, 4, 6)",
    )
    publisher.wait_for_catchup("sub1")
    assert subscriber.sql("SELECT * FROM test_part_c ORDER BY a, b") == [
        (1, None, None),
        (2, 4, None),
    ], "partitions with different replica identities not replicated correctly"

    # --- attach a partition with incompatible RI -----------------------------
    publisher.sql_batch(
        "CREATE TABLE test_part_d (a int, b int) PARTITION BY LIST (a)",
        "CREATE TABLE test_part_d_1 PARTITION OF test_part_d FOR VALUES IN (1,3)",
        "ALTER TABLE test_part_d_1 ADD PRIMARY KEY (a)",
        "ALTER TABLE test_part_d_1 REPLICA IDENTITY USING INDEX test_part_d_1_pkey",
        "INSERT INTO test_part_d VALUES (1, 2)",
    )
    subscriber.sql_batch(
        "CREATE TABLE test_part_d (a int, b int) PARTITION BY LIST (a)",
        "CREATE TABLE test_part_d_1 PARTITION OF test_part_d FOR VALUES IN (1,3)",
        "ALTER TABLE test_part_d_1 ADD PRIMARY KEY (a)",
        "ALTER TABLE test_part_d_1 REPLICA IDENTITY USING INDEX test_part_d_1_pkey",
        "CREATE TABLE test_part_d_2 PARTITION OF test_part_d FOR VALUES IN (2,4)",
        "ALTER TABLE test_part_d_2 ADD PRIMARY KEY (a)",
        "ALTER TABLE test_part_d_2 REPLICA IDENTITY USING INDEX test_part_d_2_pkey",
    )
    publisher.sql(
        "CREATE PUBLICATION pub9 FOR TABLE test_part_d (a) WITH (publish_via_partition_root = true)"
    )
    subscriber.sql("DROP SUBSCRIPTION sub1")
    subscriber.sql(f"CREATE SUBSCRIPTION sub1 CONNECTION '{connstr}' PUBLICATION pub9")
    subscriber.wait_for_subscription_sync()

    publisher.sql("INSERT INTO test_part_d VALUES (3, 4)")
    publisher.wait_for_catchup("sub1")
    assert subscriber.sql("SELECT * FROM test_part_d ORDER BY a, b") == [
        (1, None),
        (3, None),
    ], "partitions with different replica identities not replicated correctly"

    # --- table in a FOR ALL TABLES publication => all columns ----------------
    publisher.sql(
        """
        DROP TABLE tab1, tab2, tab3, tab4, tab5, tab6, tab7,
                   test_part, test_part_a, test_part_b, test_part_c, test_part_d;
        """
    )
    publisher.sql_batch(
        "CREATE TABLE test_mix_2 (a int PRIMARY KEY, b int, c int)",
        "CREATE PUBLICATION pub_mix_3 FOR TABLE test_mix_2 (a, b, c)",
        "CREATE PUBLICATION pub_mix_4 FOR ALL TABLES",
        "INSERT INTO test_mix_2 VALUES (1, 2, 3)",
    )
    subscriber.sql("CREATE TABLE test_mix_2 (a int PRIMARY KEY, b int, c int)")
    subscriber.sql("DROP SUBSCRIPTION sub1")
    subscriber.sql(
        f"CREATE SUBSCRIPTION sub1 CONNECTION '{connstr}' PUBLICATION pub_mix_3, pub_mix_4"
    )
    subscriber.wait_for_subscription_sync()

    publisher.sql("INSERT INTO test_mix_2 VALUES (4, 5, 6)")
    publisher.wait_for_catchup("sub1")
    assert subscriber.sql("SELECT * FROM test_mix_2") == [
        (1, 2, 3),
        (4, 5, 6),
    ], "all columns should be replicated"

    # --- table in a FOR TABLES IN SCHEMA publication => all columns ----------
    subscriber.sql("DROP SUBSCRIPTION sub1")
    subscriber.sql("CREATE TABLE test_mix_3 (a int PRIMARY KEY, b int, c int)")
    publisher.sql_batch(
        "DROP TABLE test_mix_2",
        "CREATE TABLE test_mix_3 (a int PRIMARY KEY, b int, c int)",
        "CREATE PUBLICATION pub_mix_5 FOR TABLE test_mix_3 (a, b, c)",
        "CREATE PUBLICATION pub_mix_6 FOR TABLES IN SCHEMA public",
        "INSERT INTO test_mix_3 VALUES (1, 2, 3)",
    )
    subscriber.sql(
        f"CREATE SUBSCRIPTION sub1 CONNECTION '{connstr}' PUBLICATION pub_mix_5, pub_mix_6"
    )
    subscriber.wait_for_subscription_sync()

    publisher.sql("INSERT INTO test_mix_3 VALUES (4, 5, 6)")
    publisher.wait_for_catchup("sub1")
    assert subscriber.sql("SELECT * FROM test_mix_3") == [
        (1, 2, 3),
        (4, 5, 6),
    ], "all columns should be replicated"

    # --- publish_via_partition_root applies only the root column list --------
    subscriber.sql("DROP SUBSCRIPTION sub1")
    subscriber.sql_batch(
        "CREATE TABLE test_root (a int PRIMARY KEY, b int, c int) PARTITION BY RANGE (a)",
        "CREATE TABLE test_root_1 PARTITION OF test_root FOR VALUES FROM (1) TO (10)",
        "CREATE TABLE test_root_2 PARTITION OF test_root FOR VALUES FROM (10) TO (20)",
    )
    publisher.sql_batch(
        "CREATE TABLE test_root (a int PRIMARY KEY, b int, c int) PARTITION BY RANGE (a)",
        "CREATE TABLE test_root_1 PARTITION OF test_root FOR VALUES FROM (1) TO (10)",
        "CREATE TABLE test_root_2 PARTITION OF test_root FOR VALUES FROM (10) TO (20)",
        "CREATE PUBLICATION pub_test_root FOR TABLE test_root (a) WITH (publish_via_partition_root = true)",
        "CREATE PUBLICATION pub_test_root_1 FOR TABLE test_root_1 (a, b)",
        "INSERT INTO test_root VALUES (1, 2, 3)",
        "INSERT INTO test_root VALUES (10, 20, 30)",
    )
    subscriber.sql(
        f"CREATE SUBSCRIPTION sub1 CONNECTION '{connstr}' "
        "PUBLICATION pub_test_root, pub_test_root_1"
    )
    subscriber.wait_for_subscription_sync()

    publisher.sql_batch(
        "INSERT INTO test_root VALUES (2, 3, 4)",
        "INSERT INTO test_root VALUES (11, 21, 31)",
    )
    publisher.wait_for_catchup("sub1")
    assert subscriber.sql("SELECT * FROM test_root ORDER BY a, b, c") == [
        (1, None, None),
        (2, None, None),
        (10, None, None),
        (11, None, None),
    ], "publication via partition root applies column list"

    # --- partition published via a schema (no list) and directly (all cols) --
    publisher.sql_batch(
        "DROP PUBLICATION pub1, pub2, pub3, pub4, pub5, pub6, pub7, pub8",
        "CREATE SCHEMA s1",
        "CREATE TABLE s1.t (a int, b int, c int) PARTITION BY RANGE (a)",
        "CREATE TABLE t_1 PARTITION OF s1.t FOR VALUES FROM (1) TO (10)",
        "CREATE PUBLICATION pub1 FOR TABLES IN SCHEMA s1",
        "CREATE PUBLICATION pub2 FOR TABLE t_1(a, b, c)",
        "INSERT INTO s1.t VALUES (1, 2, 3)",
    )
    subscriber.sql_batch(
        "CREATE SCHEMA s1",
        "CREATE TABLE s1.t (a int, b int, c int) PARTITION BY RANGE (a)",
        "CREATE TABLE t_1 PARTITION OF s1.t FOR VALUES FROM (1) TO (10)",
    )
    subscriber.sql("DROP SUBSCRIPTION sub1")
    subscriber.sql(
        f"CREATE SUBSCRIPTION sub1 CONNECTION '{connstr}' PUBLICATION pub1, pub2"
    )
    subscriber.wait_for_subscription_sync()

    publisher.sql("INSERT INTO s1.t VALUES (4, 5, 6)")
    publisher.wait_for_catchup("sub1")
    assert subscriber.sql("SELECT * FROM s1.t ORDER BY a") == [
        (1, 2, 3),
        (4, 5, 6),
    ], "two publications, publishing the same relation"

    # Resync with the publications in the opposite order; same result.
    subscriber.sql("TRUNCATE s1.t")
    subscriber.sql("ALTER SUBSCRIPTION sub1 SET PUBLICATION pub2, pub1")
    subscriber.wait_for_subscription_sync()

    publisher.sql("INSERT INTO s1.t VALUES (7, 8, 9)")
    publisher.wait_for_catchup("sub1")
    assert subscriber.sql("SELECT * FROM s1.t ORDER BY a") == (7, 8, 9), (
        "two publications, publishing the same relation"
    )

    # --- one publication with parent and child; root list "a" wins -----------
    publisher.sql_batch(
        "DROP SCHEMA s1 CASCADE",
        "CREATE TABLE t (a int, b int, c int) PARTITION BY RANGE (a)",
        "CREATE TABLE t_1 PARTITION OF t FOR VALUES FROM (1) TO (10) PARTITION BY RANGE (a)",
        "CREATE TABLE t_2 PARTITION OF t_1 FOR VALUES FROM (1) TO (10)",
        "CREATE PUBLICATION pub3 FOR TABLE t_1 (a), t_2 WITH (PUBLISH_VIA_PARTITION_ROOT)",
        "INSERT INTO t VALUES (1, 2, 3)",
    )
    subscriber.sql_batch(
        "DROP SCHEMA s1 CASCADE",
        "CREATE TABLE t (a int, b int, c int) PARTITION BY RANGE (a)",
        "CREATE TABLE t_1 PARTITION OF t FOR VALUES FROM (1) TO (10) PARTITION BY RANGE (a)",
        "CREATE TABLE t_2 PARTITION OF t_1 FOR VALUES FROM (1) TO (10)",
    )
    subscriber.sql("DROP SUBSCRIPTION sub1")
    subscriber.sql(f"CREATE SUBSCRIPTION sub1 CONNECTION '{connstr}' PUBLICATION pub3")
    subscriber.wait_for_subscription_sync()

    publisher.sql("INSERT INTO t VALUES (4, 5, 6)")
    publisher.wait_for_catchup("sub1")
    assert subscriber.sql("SELECT * FROM t ORDER BY a, b, c") == [
        (1, None, None),
        (4, None, None),
    ], "publication containing both parent and child relation"

    # Same, but now both relations have a column list defined.
    publisher.sql_batch(
        "DROP TABLE t",
        "CREATE TABLE t (a int, b int, c int) PARTITION BY RANGE (a)",
        "CREATE TABLE t_1 PARTITION OF t FOR VALUES FROM (1) TO (10) PARTITION BY RANGE (a)",
        "CREATE TABLE t_2 PARTITION OF t_1 FOR VALUES FROM (1) TO (10)",
        "CREATE PUBLICATION pub4 FOR TABLE t_1 (a), t_2 (b) WITH (PUBLISH_VIA_PARTITION_ROOT)",
        "INSERT INTO t VALUES (1, 2, 3)",
    )
    subscriber.sql_batch(
        "DROP TABLE t",
        "CREATE TABLE t (a int, b int, c int) PARTITION BY RANGE (a)",
        "CREATE TABLE t_1 PARTITION OF t FOR VALUES FROM (1) TO (10) PARTITION BY RANGE (a)",
        "CREATE TABLE t_2 PARTITION OF t_1 FOR VALUES FROM (1) TO (10)",
    )
    subscriber.sql("DROP SUBSCRIPTION sub1")
    subscriber.sql(f"CREATE SUBSCRIPTION sub1 CONNECTION '{connstr}' PUBLICATION pub4")
    subscriber.wait_for_subscription_sync()

    publisher.sql("INSERT INTO t VALUES (4, 5, 6)")
    publisher.wait_for_catchup("sub1")
    assert subscriber.sql("SELECT * FROM t ORDER BY a, b, c") == [
        (1, None, None),
        (4, None, None),
    ], "publication containing both parent and child relation"

    # --- old tuple of UPDATE/DELETE only contains column-list columns --------
    publisher.sql_batch(
        "CREATE TABLE test_oldtuple_col (a int PRIMARY KEY, b int, c int)",
        "CREATE PUBLICATION pub_check_oldtuple FOR TABLE test_oldtuple_col (a, b)",
        "INSERT INTO test_oldtuple_col VALUES(1, 2, 3)",
    )
    publisher.sql(
        "SELECT * FROM pg_create_logical_replication_slot('test_slot', 'pgoutput')"
    )
    publisher.sql_batch(
        "UPDATE test_oldtuple_col SET a = 2", "DELETE FROM test_oldtuple_col"
    )

    # Byte 7 (1-based) of the message holds the old-tuple column count; both the
    # UPDATE ('U' = 85) and DELETE ('D' = 68) messages should carry 2 columns.
    assert publisher.sql(
        """
        SELECT substr(data, 7, 2) = int2send(2::smallint)
        FROM pg_logical_slot_peek_binary_changes('test_slot', NULL, NULL,
            'proto_version', '1',
            'publication_names', 'pub_check_oldtuple')
        WHERE get_byte(data, 0) = 85 OR get_byte(data, 0) = 68
        """
    ) == [True, True], "check the number of columns in the old tuple"

    # --- dropped/generated columns are ignored for the column list -----------
    publisher.sql_batch(
        "CREATE TABLE test_mix_4 (a int PRIMARY KEY, b int, c int, d int GENERATED ALWAYS AS (a + 1) STORED, e int GENERATED ALWAYS AS (a + 2) VIRTUAL)",
        "ALTER TABLE test_mix_4 DROP COLUMN c",
        "CREATE PUBLICATION pub_mix_7 FOR TABLE test_mix_4 (a, b)",
        "CREATE PUBLICATION pub_mix_8 FOR TABLE test_mix_4",
        "INSERT INTO test_mix_4 VALUES (1, 2)",
    )
    subscriber.sql("DROP SUBSCRIPTION sub1")
    subscriber.sql("CREATE TABLE test_mix_4 (a int PRIMARY KEY, b int, c int, d int)")
    subscriber.sql(
        f"CREATE SUBSCRIPTION sub1 CONNECTION '{connstr}' PUBLICATION pub_mix_7, pub_mix_8"
    )
    subscriber.wait_for_subscription_sync()
    assert subscriber.sql("SELECT * FROM test_mix_4 ORDER BY a") == (
        1,
        2,
        None,
        None,
    ), "initial synchronization with multiple publications with the same column list"

    publisher.sql("INSERT INTO test_mix_4 VALUES (3, 4)")
    publisher.wait_for_catchup("sub1")
    assert subscriber.sql("SELECT * FROM test_mix_4 ORDER BY a") == [
        (1, 2, None, None),
        (3, 4, None, None),
    ], "replication with multiple publications with the same column list"

    # --- different column lists in two publications: error on subscribe ------
    publisher.sql_batch(
        "CREATE TABLE test_mix_1 (a int PRIMARY KEY, b int, c int)",
        "CREATE PUBLICATION pub_mix_1 FOR TABLE test_mix_1 (a, b)",
        "CREATE PUBLICATION pub_mix_2 FOR TABLE test_mix_1 (a, c)",
    )
    subscriber.sql("DROP SUBSCRIPTION sub1")
    subscriber.sql("CREATE TABLE test_mix_1 (a int PRIMARY KEY, b int, c int)")
    with pytest.raises(
        LibpqError,
        match=r'cannot use different column lists for table "public.test_mix_1" in different publications',
    ):
        subscriber.sql(
            f"CREATE SUBSCRIPTION sub1 CONNECTION '{connstr}' PUBLICATION pub_mix_1, pub_mix_2"
        )

    # --- column list changed after subscribing: walsender reports the error --
    publisher.sql("ALTER PUBLICATION pub_mix_1 SET TABLE test_mix_1 (a, c)")
    subscriber.sql(
        f"CREATE SUBSCRIPTION sub1 CONNECTION '{connstr}' PUBLICATION pub_mix_1, pub_mix_2"
    )
    publisher.wait_for_catchup("sub1")

    offset = publisher.current_log_position()
    publisher.sql_batch(
        "ALTER PUBLICATION pub_mix_1 SET TABLE test_mix_1 (a, b)",
        "INSERT INTO test_mix_1 VALUES(1, 1, 1)",
    )
    publisher.wait_for_log(
        r'cannot use different column lists for table "public.test_mix_1" in different publications',
        offset,
    )
