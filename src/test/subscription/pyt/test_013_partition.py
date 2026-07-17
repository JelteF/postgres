# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/013_partition.pl.

Tests logical replication with partitioned tables. subscriber1 is partitioned
differently from the publisher (exercising tuple routing and sub-partitioning);
subscriber2 is non-partitioned with tables matching the leaf partitions. Covers
replication via leaf-partition identity and via the partition root
(publish_via_partition_root), INSERT/UPDATE/DELETE (including cross-partition
updates done as delete+insert), TRUNCATE, AFTER REPLICA triggers, column-order
and column-add changes on both sides, and update_missing / delete_missing /
update_origin_differs conflict logging.
"""

import re


def test_partition(create_pg):
    publisher = create_pg("publisher", allows_streaming="logical")
    subscriber1 = create_pg("subscriber1")
    subscriber2 = create_pg("subscriber2")
    connstr = publisher.connstr()

    # --- publisher -----------------------------------------------------------
    publisher.sql("CREATE PUBLICATION pub1")
    publisher.sql("CREATE PUBLICATION pub_all FOR ALL TABLES")
    publisher.sql_batch(
        "CREATE TABLE tab1 (a int PRIMARY KEY, b text) PARTITION BY LIST (a)",
        "CREATE TABLE tab1_1 (b text, a int NOT NULL)",
        "ALTER TABLE tab1 ATTACH PARTITION tab1_1 FOR VALUES IN (1, 2, 3)",
        "CREATE TABLE tab1_2 PARTITION OF tab1 FOR VALUES IN (4, 5, 6)",
        "CREATE TABLE tab1_def PARTITION OF tab1 DEFAULT",
    )
    publisher.sql("ALTER PUBLICATION pub1 ADD TABLE tab1, tab1_1")

    # --- subscriber1: partitioned differently, tab1_2 sub-partitioned --------
    subscriber1.sql_batch(
        "CREATE TABLE tab1 (c text, a int PRIMARY KEY, b text) PARTITION BY LIST (a)",
        "CREATE INDEX tab1_c_brin_idx ON tab1 USING brin (c)",
        "CREATE TABLE tab1_1 (b text, c text DEFAULT 'sub1_tab1', a int NOT NULL)",
        "ALTER TABLE tab1 ATTACH PARTITION tab1_1 FOR VALUES IN (1, 2, 3)",
        "CREATE TABLE tab1_2 PARTITION OF tab1 (c DEFAULT 'sub1_tab1') FOR VALUES IN (4, 5, 6) PARTITION BY LIST (a)",
        "CREATE TABLE tab1_2_1 (c text, b text, a int NOT NULL)",
        "ALTER TABLE tab1_2 ATTACH PARTITION tab1_2_1 FOR VALUES IN (5)",
        "CREATE TABLE tab1_2_2 PARTITION OF tab1_2 FOR VALUES IN (4, 6)",
        "CREATE TABLE tab1_def PARTITION OF tab1 (c DEFAULT 'sub1_tab1') DEFAULT",
    )
    subscriber1.sql(f"CREATE SUBSCRIPTION sub1 CONNECTION '{connstr}' PUBLICATION pub1")

    # AFTER replica triggers recording all trigger activity, enabled for a
    # subset of the partition tree.
    subscriber1.sql_batch(
        """
        CREATE TABLE sub1_trigger_activity (tgtab text, tgop text,
          tgwhen text, tglevel text, olda int, newa int)
        """,
        """
        CREATE FUNCTION sub1_trigger_activity_func() RETURNS TRIGGER AS $$
        BEGIN
          IF (TG_OP = 'INSERT') THEN
            INSERT INTO public.sub1_trigger_activity
              SELECT TG_RELNAME, TG_OP, TG_WHEN, TG_LEVEL, NULL, NEW.a;
          ELSIF (TG_OP = 'UPDATE') THEN
            INSERT INTO public.sub1_trigger_activity
              SELECT TG_RELNAME, TG_OP, TG_WHEN, TG_LEVEL, OLD.a, NEW.a;
          END IF;
          RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
        """,
        """
        CREATE TRIGGER sub1_tab1_log_op_trigger
          AFTER INSERT OR UPDATE ON tab1
          FOR EACH ROW EXECUTE PROCEDURE sub1_trigger_activity_func()
        """,
        "ALTER TABLE ONLY tab1 ENABLE REPLICA TRIGGER sub1_tab1_log_op_trigger",
        """
        CREATE TRIGGER sub1_tab1_2_log_op_trigger
          AFTER INSERT OR UPDATE ON tab1_2
          FOR EACH ROW EXECUTE PROCEDURE sub1_trigger_activity_func()
        """,
        "ALTER TABLE ONLY tab1_2 ENABLE REPLICA TRIGGER sub1_tab1_2_log_op_trigger",
        """
        CREATE TRIGGER sub1_tab1_2_2_log_op_trigger
          AFTER INSERT OR UPDATE ON tab1_2_2
          FOR EACH ROW EXECUTE PROCEDURE sub1_trigger_activity_func()
        """,
        "ALTER TABLE ONLY tab1_2_2 ENABLE REPLICA TRIGGER sub1_tab1_2_2_log_op_trigger",
    )

    # --- subscriber2: non-partitioned, matches leaf tables -------------------
    subscriber2.sql(
        "CREATE TABLE tab1 (a int PRIMARY KEY, c text DEFAULT 'sub2_tab1', b text)"
    )
    subscriber2.sql(
        "CREATE TABLE tab1_1 (a int PRIMARY KEY, c text DEFAULT 'sub2_tab1_1', b text)"
    )
    subscriber2.sql(
        "CREATE TABLE tab1_2 (a int PRIMARY KEY, c text DEFAULT 'sub2_tab1_2', b text)"
    )
    subscriber2.sql(
        "CREATE TABLE tab1_def (a int PRIMARY KEY, b text, c text DEFAULT 'sub2_tab1_def')"
    )
    subscriber2.sql(
        f"CREATE SUBSCRIPTION sub2 CONNECTION '{connstr}' PUBLICATION pub_all"
    )
    subscriber2.sql_batch(
        """
        CREATE TABLE sub2_trigger_activity (tgtab text,
          tgop text, tgwhen text, tglevel text, olda int, newa int)
        """,
        """
        CREATE FUNCTION sub2_trigger_activity_func() RETURNS TRIGGER AS $$
        BEGIN
          IF (TG_OP = 'INSERT') THEN
            INSERT INTO public.sub2_trigger_activity
              SELECT TG_RELNAME, TG_OP, TG_WHEN, TG_LEVEL, NULL, NEW.a;
          ELSIF (TG_OP = 'UPDATE') THEN
            INSERT INTO public.sub2_trigger_activity
              SELECT TG_RELNAME, TG_OP, TG_WHEN, TG_LEVEL, OLD.a, NEW.a;
          END IF;
          RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
        """,
        """
        CREATE TRIGGER sub2_tab1_log_op_trigger
          AFTER INSERT OR UPDATE ON tab1
          FOR EACH ROW EXECUTE PROCEDURE sub2_trigger_activity_func()
        """,
        "ALTER TABLE ONLY tab1 ENABLE REPLICA TRIGGER sub2_tab1_log_op_trigger",
        """
        CREATE TRIGGER sub2_tab1_2_log_op_trigger
          AFTER INSERT OR UPDATE ON tab1_2
          FOR EACH ROW EXECUTE PROCEDURE sub2_trigger_activity_func()
        """,
        "ALTER TABLE ONLY tab1_2 ENABLE REPLICA TRIGGER sub2_tab1_2_log_op_trigger",
    )

    subscriber1.wait_for_subscription_sync()
    subscriber2.wait_for_subscription_sync()

    # ===== replication using leaf partition identity and schema =============

    # insert
    publisher.sql("INSERT INTO tab1 VALUES (1)")
    publisher.sql("INSERT INTO tab1_1 (a) VALUES (3)")
    publisher.sql("INSERT INTO tab1_2 VALUES (5)")
    publisher.sql("INSERT INTO tab1 VALUES (0)")
    publisher.wait_for_catchup("sub1")
    publisher.wait_for_catchup("sub2")

    assert subscriber1.sql("SELECT c, a FROM tab1 ORDER BY 1, 2") == [
        ("sub1_tab1", 0),
        ("sub1_tab1", 1),
        ("sub1_tab1", 3),
        ("sub1_tab1", 5),
    ], "inserts into tab1 and its partitions replicated"
    assert subscriber1.sql("SELECT a FROM tab1_2_1 ORDER BY 1") == 5, (
        "inserts into tab1_2 replicated into tab1_2_1 correctly"
    )
    assert subscriber1.sql("SELECT a FROM tab1_2_2 ORDER BY 1") == [], (
        "inserts into tab1_2 replicated into tab1_2_2 correctly"
    )
    assert subscriber2.sql("SELECT c, a FROM tab1_1 ORDER BY 1, 2") == [
        ("sub2_tab1_1", 1),
        ("sub2_tab1_1", 3),
    ], "inserts into tab1_1 replicated"
    assert subscriber2.sql("SELECT c, a FROM tab1_2 ORDER BY 1, 2") == (
        "sub2_tab1_2",
        5,
    ), "inserts into tab1_2 replicated"
    assert subscriber2.sql(
        "SELECT * FROM sub2_trigger_activity ORDER BY tgtab, tgop, tgwhen, olda, newa"
    ) == ("tab1_2", "INSERT", "AFTER", "ROW", None, 5), (
        "check replica insert after trigger applied on subscriber"
    )
    assert subscriber2.sql("SELECT c, a FROM tab1_def ORDER BY 1, 2") == (
        "sub2_tab1_def",
        0,
    ), "inserts into tab1_def replicated"

    # update (replicated as update)
    publisher.sql("UPDATE tab1 SET a = 2 WHERE a = 1")
    # These cause an update applied to a partitioned table on subscriber1:
    # tab1_2 is a leaf on the publisher but sub-partitioned on subscriber1.
    publisher.sql("UPDATE tab1 SET a = 6 WHERE a = 5")
    publisher.sql("UPDATE tab1 SET a = 4 WHERE a = 6")
    publisher.sql("UPDATE tab1 SET a = 6 WHERE a = 4")
    publisher.wait_for_catchup("sub1")
    publisher.wait_for_catchup("sub2")

    assert subscriber1.sql("SELECT c, a FROM tab1 ORDER BY 1, 2") == [
        ("sub1_tab1", 0),
        ("sub1_tab1", 2),
        ("sub1_tab1", 3),
        ("sub1_tab1", 6),
    ], "update of tab1_1, tab1_2 replicated"
    assert subscriber1.sql("SELECT a FROM tab1_2_1 ORDER BY 1") == [], (
        "updates of tab1_2 replicated into tab1_2_1 correctly"
    )
    assert subscriber1.sql("SELECT a FROM tab1_2_2 ORDER BY 1") == 6, (
        "updates of tab1_2 replicated into tab1_2_2 correctly"
    )
    assert subscriber1.sql(
        "SELECT * FROM sub1_trigger_activity ORDER BY tgtab, tgop, tgwhen, olda, newa"
    ) == [
        ("tab1_2_2", "INSERT", "AFTER", "ROW", None, 6),
        ("tab1_2_2", "UPDATE", "AFTER", "ROW", 4, 6),
        ("tab1_2_2", "UPDATE", "AFTER", "ROW", 6, 4),
    ], "check replica update after trigger applied on subscriber"
    assert subscriber2.sql("SELECT c, a FROM tab1_1 ORDER BY 1, 2") == [
        ("sub2_tab1_1", 2),
        ("sub2_tab1_1", 3),
    ], "update of tab1_1 replicated"
    assert subscriber2.sql("SELECT c, a FROM tab1_2 ORDER BY 1, 2") == (
        "sub2_tab1_2",
        6,
    ), "tab1_2 updated"
    assert subscriber2.sql(
        "SELECT * FROM sub2_trigger_activity ORDER BY tgtab, tgop, tgwhen, olda, newa"
    ) == [
        ("tab1_2", "INSERT", "AFTER", "ROW", None, 5),
        ("tab1_2", "UPDATE", "AFTER", "ROW", 4, 6),
        ("tab1_2", "UPDATE", "AFTER", "ROW", 5, 6),
        ("tab1_2", "UPDATE", "AFTER", "ROW", 6, 4),
    ], "check replica update after trigger applied on subscriber"
    assert subscriber2.sql("SELECT c, a FROM tab1_def ORDER BY 1") == (
        "sub2_tab1_def",
        0,
    ), "tab1_def unchanged"

    # update (replicated as delete+insert)
    publisher.sql("UPDATE tab1 SET a = 1 WHERE a = 0")
    publisher.sql("UPDATE tab1 SET a = 4 WHERE a = 1")
    publisher.wait_for_catchup("sub1")
    publisher.wait_for_catchup("sub2")

    assert subscriber1.sql("SELECT c, a FROM tab1 ORDER BY 1, 2") == [
        ("sub1_tab1", 2),
        ("sub1_tab1", 3),
        ("sub1_tab1", 4),
        ("sub1_tab1", 6),
    ], "update of tab1 (delete from tab1_def + insert into tab1_1) replicated"
    assert subscriber1.sql("SELECT a FROM tab1_2_2 ORDER BY 1") == [4, 6], (
        "updates of tab1 (delete + insert) replicated into tab1_2_2 correctly"
    )
    assert subscriber2.sql("SELECT c, a FROM tab1_1 ORDER BY 1, 2") == [
        ("sub2_tab1_1", 2),
        ("sub2_tab1_1", 3),
    ], "tab1_1 unchanged"
    assert subscriber2.sql("SELECT c, a FROM tab1_2 ORDER BY 1, 2") == [
        ("sub2_tab1_2", 4),
        ("sub2_tab1_2", 6),
    ], "insert into tab1_2 replicated"
    assert subscriber2.sql("SELECT a FROM tab1_def ORDER BY 1") == [], (
        "delete from tab1_def replicated"
    )

    # delete
    publisher.sql("DELETE FROM tab1 WHERE a IN (2, 3, 5)")
    publisher.sql("DELETE FROM tab1_2")
    publisher.wait_for_catchup("sub1")
    publisher.wait_for_catchup("sub2")

    assert subscriber1.sql("SELECT a FROM tab1") == [], (
        "delete from tab1_1, tab1_2 replicated"
    )
    assert subscriber2.sql("SELECT a FROM tab1_1") == [], (
        "delete from tab1_1 replicated"
    )
    assert subscriber2.sql("SELECT a FROM tab1_2") == [], (
        "delete from tab1_2 replicated"
    )

    # truncate
    subscriber1.sql("INSERT INTO tab1 (a) VALUES (1), (2), (5)")
    subscriber2.sql("INSERT INTO tab1_2 (a) VALUES (2)")
    publisher.sql("TRUNCATE tab1_2")
    publisher.wait_for_catchup("sub1")
    publisher.wait_for_catchup("sub2")

    assert subscriber1.sql("SELECT a FROM tab1 ORDER BY 1") == [1, 2], (
        "truncate of tab1_2 replicated"
    )
    assert subscriber2.sql("SELECT a FROM tab1_2 ORDER BY 1") == [], (
        "truncate of tab1_2 replicated"
    )

    publisher.sql("TRUNCATE tab1")
    publisher.wait_for_catchup("sub1")
    publisher.wait_for_catchup("sub2")

    assert subscriber1.sql("SELECT a FROM tab1 ORDER BY 1") == [], (
        "truncate of tab1_1 replicated"
    )
    assert subscriber2.sql("SELECT a FROM tab1 ORDER BY 1") == [], (
        "truncate of tab1 replicated"
    )

    publisher.sql("INSERT INTO tab1 VALUES (1, 'foo'), (4, 'bar'), (10, 'baz')")
    publisher.wait_for_catchup("sub1")
    publisher.wait_for_catchup("sub2")

    subscriber1.sql("DELETE FROM tab1")

    # Grab the log position after a query has run, so the config reload (here,
    # just a known starting point) has surely taken effect.
    log_location = subscriber1.current_log_position()

    publisher.sql("UPDATE tab1 SET b = 'quux' WHERE a = 4")
    publisher.sql("DELETE FROM tab1")
    publisher.wait_for_catchup("sub1")
    publisher.wait_for_catchup("sub2")

    logtext = subscriber1.log_since(log_location)
    assert re.search(
        r'conflict detected on relation "public.tab1_2_2": conflict=update_missing.*\n'
        r".*DETAIL:.* Could not find the row to be updated: "
        r"remote row \(null, 4, quux\), replica identity \(a\)=\(4\)",
        logtext,
    ), "update target row is missing in tab1_2_2"
    assert re.search(
        r'conflict detected on relation "public.tab1_1": conflict=delete_missing.*\n'
        r".*DETAIL:.* Could not find the row to be deleted: replica identity \(a\)=\(1\)",
        logtext,
    ), "delete target row is missing in tab1_1"
    assert re.search(
        r'conflict detected on relation "public.tab1_2_2": conflict=delete_missing.*\n'
        r".*DETAIL:.* Could not find the row to be deleted: replica identity \(a\)=\(4\)",
        logtext,
    ), "delete target row is missing in tab1_2_2"
    assert re.search(
        r'conflict detected on relation "public.tab1_def": conflict=delete_missing.*\n'
        r".*DETAIL:.* Could not find the row to be deleted: replica identity \(a\)=\(10\)",
        logtext,
    ), "delete target row is missing in tab1_def"

    # ===== replication using root table identity and schema =================

    publisher.sql("DROP PUBLICATION pub1")
    publisher.sql_batch(
        "CREATE TABLE tab2 (a int PRIMARY KEY, b text) PARTITION BY LIST (a)",
        "CREATE TABLE tab2_1 (b text, a int NOT NULL)",
        "ALTER TABLE tab2 ATTACH PARTITION tab2_1 FOR VALUES IN (0, 1, 2, 3)",
        "CREATE TABLE tab2_2 PARTITION OF tab2 FOR VALUES IN (5, 6)",
        "CREATE TABLE tab3 (a int PRIMARY KEY, b text) PARTITION BY LIST (a)",
        "CREATE TABLE tab3_1 PARTITION OF tab3 FOR VALUES IN (0, 1, 2, 3, 5, 6)",
        "CREATE TABLE tab4 (a int PRIMARY KEY) PARTITION BY LIST (a)",
        "CREATE TABLE tab4_1 PARTITION OF tab4 FOR VALUES IN (-1, 0, 1) PARTITION BY LIST (a)",
        "CREATE TABLE tab4_1_1 PARTITION OF tab4_1 FOR VALUES IN (-1, 0, 1)",
    )
    publisher.sql("ALTER PUBLICATION pub_all SET (publish_via_partition_root = true)")
    # tab3_1's parent is not in the publication, so its changes use its own
    # identity. For tab2, parent and child are both present but changes
    # replicate via the parent's identity, only once.
    publisher.sql(
        "CREATE PUBLICATION pub_viaroot FOR TABLE tab2, tab2_1, tab3_1 "
        "WITH (publish_via_partition_root = true)"
    )
    publisher.sql(
        "CREATE PUBLICATION pub_lower_level FOR TABLE tab4_1 "
        "WITH (publish_via_partition_root = true)"
    )

    publisher.sql("INSERT INTO tab2 VALUES (1)")
    publisher.sql("INSERT INTO tab4 VALUES (-1)")

    # subscriber 1
    subscriber1.sql("DROP SUBSCRIPTION sub1")
    subscriber1.sql_batch(
        "CREATE TABLE tab2 (a int PRIMARY KEY, c text DEFAULT 'sub1_tab2', b text) PARTITION BY RANGE (a)",
        "CREATE TABLE tab2_1 (c text DEFAULT 'sub1_tab2', b text, a int NOT NULL)",
        "ALTER TABLE tab2 ATTACH PARTITION tab2_1 FOR VALUES FROM (0) TO (10)",
        "CREATE TABLE tab3_1 (c text DEFAULT 'sub1_tab3_1', b text, a int NOT NULL PRIMARY KEY)",
    )
    subscriber1.sql(
        f"CREATE SUBSCRIPTION sub_viaroot CONNECTION '{connstr}' PUBLICATION pub_viaroot"
    )

    # subscriber 2
    subscriber2.sql("DROP TABLE tab1")
    subscriber2.sql_batch(
        "CREATE TABLE tab1 (a int PRIMARY KEY, c text DEFAULT 'sub2_tab1', b text) PARTITION BY HASH (a)",
        "CREATE TABLE tab1_part1 (b text, c text, a int NOT NULL)",
        "ALTER TABLE tab1 ATTACH PARTITION tab1_part1 FOR VALUES WITH (MODULUS 2, REMAINDER 0)",
        "CREATE TABLE tab1_part2 PARTITION OF tab1 FOR VALUES WITH (MODULUS 2, REMAINDER 1)",
        "CREATE TABLE tab2 (a int PRIMARY KEY, c text DEFAULT 'sub2_tab2', b text)",
        "CREATE TABLE tab3 (a int PRIMARY KEY, c text DEFAULT 'sub2_tab3', b text)",
        "CREATE TABLE tab3_1 (a int PRIMARY KEY, c text DEFAULT 'sub2_tab3_1', b text)",
        "CREATE TABLE tab4 (a int PRIMARY KEY)",
        "CREATE TABLE tab4_1 (a int PRIMARY KEY)",
    )
    # Both publications use publish_via_partition_root, so partitions use their
    # root tables' identity. The FOR ALL TABLES publication is listed second.
    subscriber2.sql("ALTER SUBSCRIPTION sub2 SET PUBLICATION pub_lower_level, pub_all")

    subscriber1.wait_for_subscription_sync()
    subscriber2.wait_for_subscription_sync()

    assert subscriber1.sql("SELECT c, a FROM tab2") == ("sub1_tab2", 1), (
        "initial data synced for pub_viaroot"
    )
    assert subscriber2.sql("SELECT a FROM tab4 ORDER BY 1") == -1, (
        "initial data synced for pub_lower_level and pub_all"
    )
    assert subscriber2.sql("SELECT a FROM tab4_1 ORDER BY 1") == [], (
        "initial data synced for pub_lower_level and pub_all"
    )

    # insert
    publisher.sql("INSERT INTO tab1 VALUES (1), (0)")
    publisher.sql("INSERT INTO tab1_1 (a) VALUES (3)")
    publisher.sql("INSERT INTO tab1_2 VALUES (5)")
    publisher.sql("INSERT INTO tab2 VALUES (0), (3), (5)")
    publisher.sql("INSERT INTO tab3 VALUES (1), (0), (3), (5)")
    # Replicated through the partition root (FOR ALL TABLES partition).
    publisher.sql("INSERT INTO tab4 VALUES (0)")
    publisher.wait_for_catchup("sub_viaroot")
    publisher.wait_for_catchup("sub2")

    assert subscriber1.sql("SELECT c, a FROM tab2 ORDER BY 1, 2") == [
        ("sub1_tab2", 0),
        ("sub1_tab2", 1),
        ("sub1_tab2", 3),
        ("sub1_tab2", 5),
    ], "inserts into tab2 replicated"
    assert subscriber1.sql("SELECT c, a FROM tab3_1 ORDER BY 1, 2") == [
        ("sub1_tab3_1", 0),
        ("sub1_tab3_1", 1),
        ("sub1_tab3_1", 3),
        ("sub1_tab3_1", 5),
    ], "inserts into tab3_1 replicated"
    assert subscriber2.sql("SELECT c, a FROM tab1 ORDER BY 1, 2") == [
        ("sub2_tab1", 0),
        ("sub2_tab1", 1),
        ("sub2_tab1", 3),
        ("sub2_tab1", 5),
    ], "inserts into tab1 replicated"
    assert subscriber2.sql("SELECT c, a FROM tab2 ORDER BY 1, 2") == [
        ("sub2_tab2", 0),
        ("sub2_tab2", 1),
        ("sub2_tab2", 3),
        ("sub2_tab2", 5),
    ], "inserts into tab2 replicated"
    assert subscriber2.sql("SELECT c, a FROM tab3 ORDER BY 1, 2") == [
        ("sub2_tab3", 0),
        ("sub2_tab3", 1),
        ("sub2_tab3", 3),
        ("sub2_tab3", 5),
    ], "inserts into tab3 replicated"
    # tab4 replicates through the root partition -> tab4 on subscriber.
    assert subscriber2.sql("SELECT a FROM tab4 ORDER BY 1") == [-1, 0], (
        "inserts into tab4 replicated"
    )
    assert subscriber2.sql("SELECT a FROM tab4_1 ORDER BY 1") == [], (
        "inserts into tab4_1 replicated"
    )

    # switch the order of publications; result must be the same
    subscriber2.sql("ALTER SUBSCRIPTION sub2 SET PUBLICATION pub_all, pub_lower_level")
    subscriber2.wait_for_subscription_sync()

    publisher.sql("INSERT INTO tab4 VALUES (1)")
    publisher.wait_for_catchup("sub2")

    assert subscriber2.sql("SELECT a FROM tab4 ORDER BY 1") == [-1, 0, 1], (
        "inserts into tab4 replicated"
    )
    assert subscriber2.sql("SELECT a FROM tab4_1 ORDER BY 1") == [], (
        "inserts into tab4_1 replicated"
    )

    # update (replicated as update)
    publisher.sql("UPDATE tab1 SET a = 6 WHERE a = 5")
    publisher.sql("UPDATE tab2 SET a = 6 WHERE a = 5")
    publisher.sql("UPDATE tab3 SET a = 6 WHERE a = 5")
    publisher.wait_for_catchup("sub_viaroot")
    publisher.wait_for_catchup("sub2")

    assert subscriber1.sql("SELECT c, a FROM tab2 ORDER BY 1, 2") == [
        ("sub1_tab2", 0),
        ("sub1_tab2", 1),
        ("sub1_tab2", 3),
        ("sub1_tab2", 6),
    ], "update of tab2 replicated"
    assert subscriber1.sql("SELECT c, a FROM tab3_1 ORDER BY 1, 2") == [
        ("sub1_tab3_1", 0),
        ("sub1_tab3_1", 1),
        ("sub1_tab3_1", 3),
        ("sub1_tab3_1", 6),
    ], "update of tab3_1 replicated"
    assert subscriber2.sql("SELECT c, a FROM tab1 ORDER BY 1, 2") == [
        ("sub2_tab1", 0),
        ("sub2_tab1", 1),
        ("sub2_tab1", 3),
        ("sub2_tab1", 6),
    ], "inserts into tab1 replicated"
    assert subscriber2.sql("SELECT c, a FROM tab2 ORDER BY 1, 2") == [
        ("sub2_tab2", 0),
        ("sub2_tab2", 1),
        ("sub2_tab2", 3),
        ("sub2_tab2", 6),
    ], "inserts into tab2 replicated"
    assert subscriber2.sql("SELECT c, a FROM tab3 ORDER BY 1, 2") == [
        ("sub2_tab3", 0),
        ("sub2_tab3", 1),
        ("sub2_tab3", 3),
        ("sub2_tab3", 6),
    ], "inserts into tab3 replicated"

    # update (replicated as delete+insert)
    publisher.sql("UPDATE tab1 SET a = 2 WHERE a = 6")
    publisher.sql("UPDATE tab2 SET a = 2 WHERE a = 6")
    publisher.sql("UPDATE tab3 SET a = 2 WHERE a = 6")
    publisher.wait_for_catchup("sub_viaroot")
    publisher.wait_for_catchup("sub2")

    assert subscriber1.sql("SELECT c, a FROM tab2 ORDER BY 1, 2") == [
        ("sub1_tab2", 0),
        ("sub1_tab2", 1),
        ("sub1_tab2", 2),
        ("sub1_tab2", 3),
    ], "update of tab2 replicated"
    assert subscriber1.sql("SELECT c, a FROM tab3_1 ORDER BY 1, 2") == [
        ("sub1_tab3_1", 0),
        ("sub1_tab3_1", 1),
        ("sub1_tab3_1", 2),
        ("sub1_tab3_1", 3),
    ], "update of tab3_1 replicated"
    assert subscriber2.sql("SELECT c, a FROM tab1 ORDER BY 1, 2") == [
        ("sub2_tab1", 0),
        ("sub2_tab1", 1),
        ("sub2_tab1", 2),
        ("sub2_tab1", 3),
    ], "update of tab1 replicated"
    assert subscriber2.sql("SELECT c, a FROM tab2 ORDER BY 1, 2") == [
        ("sub2_tab2", 0),
        ("sub2_tab2", 1),
        ("sub2_tab2", 2),
        ("sub2_tab2", 3),
    ], "update of tab2 replicated"
    assert subscriber2.sql("SELECT c, a FROM tab3 ORDER BY 1, 2") == [
        ("sub2_tab3", 0),
        ("sub2_tab3", 1),
        ("sub2_tab3", 2),
        ("sub2_tab3", 3),
    ], "update of tab3 replicated"

    # delete
    publisher.sql("DELETE FROM tab1")
    publisher.sql("DELETE FROM tab2")
    publisher.sql("DELETE FROM tab3")
    publisher.wait_for_catchup("sub_viaroot")
    publisher.wait_for_catchup("sub2")

    assert subscriber1.sql("SELECT a FROM tab2") == [], "delete tab2 replicated"
    assert subscriber2.sql("SELECT a FROM tab1") == [], "delete from tab1 replicated"
    assert subscriber2.sql("SELECT a FROM tab2") == [], "delete from tab2 replicated"
    assert subscriber2.sql("SELECT a FROM tab3") == [], "delete from tab3 replicated"

    # truncate
    publisher.sql("INSERT INTO tab1 VALUES (1), (2), (5)")
    publisher.sql("INSERT INTO tab2 VALUES (1), (2), (5)")
    # these will NOT be replicated (publish_via_partition_root)
    publisher.sql("TRUNCATE tab1_2, tab2_1, tab3_1")
    publisher.wait_for_catchup("sub_viaroot")
    publisher.wait_for_catchup("sub2")

    assert subscriber1.sql("SELECT a FROM tab2 ORDER BY 1") == [1, 2, 5], (
        "truncate of tab2_1 NOT replicated"
    )
    assert subscriber2.sql("SELECT a FROM tab1 ORDER BY 1") == [1, 2, 5], (
        "truncate of tab1_2 NOT replicated"
    )
    assert subscriber2.sql("SELECT a FROM tab2 ORDER BY 1") == [1, 2, 5], (
        "truncate of tab2_1 NOT replicated"
    )

    publisher.sql("TRUNCATE tab1, tab2, tab3")
    publisher.wait_for_catchup("sub_viaroot")
    publisher.wait_for_catchup("sub2")

    assert subscriber1.sql("SELECT a FROM tab2") == [], "truncate of tab2 replicated"
    assert subscriber2.sql("SELECT a FROM tab1") == [], "truncate of tab1 replicated"
    assert subscriber2.sql("SELECT a FROM tab2") == [], "truncate of tab2 replicated"
    assert subscriber2.sql("SELECT a FROM tab3") == [], "truncate of tab3 replicated"
    assert subscriber2.sql("SELECT a FROM tab3_1") == [], (
        "truncate of tab3_1 replicated"
    )

    # check the leaf->root tuple conversion map is rebuilt when a column is added
    publisher.sql(
        "ALTER TABLE tab2 DROP b, ADD COLUMN c text DEFAULT 'pub_tab2', ADD b text"
    )
    publisher.sql("INSERT INTO tab2 (a, b) VALUES (1, 'xxx'), (3, 'yyy'), (5, 'zzz')")
    publisher.sql("INSERT INTO tab2 (a, b, c) VALUES (6, 'aaa', 'xxx_c')")
    publisher.wait_for_catchup("sub_viaroot")
    publisher.wait_for_catchup("sub2")

    assert subscriber1.sql("SELECT c, a, b FROM tab2 ORDER BY 1, 2") == [
        ("pub_tab2", 1, "xxx"),
        ("pub_tab2", 3, "yyy"),
        ("pub_tab2", 5, "zzz"),
        ("xxx_c", 6, "aaa"),
    ], "inserts into tab2 replicated"
    assert subscriber2.sql("SELECT c, a, b FROM tab2 ORDER BY 1, 2") == [
        ("pub_tab2", 1, "xxx"),
        ("pub_tab2", 3, "yyy"),
        ("pub_tab2", 5, "zzz"),
        ("xxx_c", 6, "aaa"),
    ], "inserts into tab2 replicated"

    subscriber1.sql("DELETE FROM tab2")
    log_location = subscriber1.current_log_position()

    publisher.sql("UPDATE tab2 SET b = 'quux' WHERE a = 5")
    publisher.sql("DELETE FROM tab2 WHERE a = 1")
    publisher.wait_for_catchup("sub_viaroot")
    publisher.wait_for_catchup("sub2")

    logtext = subscriber1.log_since(log_location)
    assert re.search(
        r'conflict detected on relation "public.tab2_1": conflict=update_missing.*\n'
        r".*DETAIL:.* Could not find the row to be updated: "
        r"remote row \(pub_tab2, quux, 5\), replica identity \(a\)=\(5\)",
        logtext,
    ), "update target row is missing in tab2_1"
    assert re.search(
        r'conflict detected on relation "public.tab2_1": conflict=delete_missing.*\n'
        r".*DETAIL:.* Could not find the row to be deleted: replica identity \(a\)=\(1\)",
        logtext,
    ), "delete target row is missing in tab2_1"

    # track_commit_timestamp lets subscriber1 detect origin-difference conflicts.
    subscriber1.append_conf(track_commit_timestamp=True)
    subscriber1.pg_ctl("restart")

    subscriber1.sql("INSERT INTO tab2 VALUES (3, 'yyy')")
    publisher.sql("UPDATE tab2 SET b = 'quux' WHERE a = 3")
    publisher.wait_for_catchup("sub_viaroot")

    logtext = subscriber1.log_since(log_location)
    assert re.search(
        r'conflict detected on relation "public.tab2_1": conflict=update_origin_differs.*\n'
        r".*DETAIL:.* Updating the row that was modified locally in transaction [0-9]+ at .*: "
        r"local row \(yyy, null, 3\), remote row \(pub_tab2, quux, 3\), replica identity \(a\)=\(3\)\.",
        logtext,
    ), "updating a row that was modified by a different origin"

    # The remaining tests no longer test conflict detection.
    subscriber1.append_conf(track_commit_timestamp=False)
    subscriber1.pg_ctl("restart")

    # ===== altering the target partitioned table still replicates ===========
    publisher.sql_batch(
        "CREATE TABLE tab5 (a int NOT NULL, b int)",
        "CREATE UNIQUE INDEX tab5_a_idx ON tab5 (a)",
        "ALTER TABLE tab5 REPLICA IDENTITY USING INDEX tab5_a_idx",
    )
    subscriber2.sql_batch(
        "CREATE TABLE tab5 (a int NOT NULL, b int, c int) PARTITION BY LIST (a)",
        "CREATE TABLE tab5_1 PARTITION OF tab5 DEFAULT",
        "CREATE UNIQUE INDEX tab5_a_idx ON tab5 (a)",
        "ALTER TABLE tab5 REPLICA IDENTITY USING INDEX tab5_a_idx",
        "ALTER TABLE tab5_1 REPLICA IDENTITY USING INDEX tab5_1_a_idx",
    )
    subscriber2.sql("ALTER SUBSCRIPTION sub2 REFRESH PUBLICATION")
    subscriber2.wait_for_subscription_sync()

    # Make the partition map cache.
    publisher.sql("INSERT INTO tab5 VALUES (1, 1)")
    publisher.sql("UPDATE tab5 SET a = 2 WHERE a = 1")
    publisher.wait_for_catchup("sub2")
    assert subscriber2.sql("SELECT a, b FROM tab5 ORDER BY 1") == (2, 1), (
        "updates of tab5 replicated correctly"
    )

    # Change the column order of the partition on the subscriber.
    subscriber2.sql_batch(
        "ALTER TABLE tab5 DETACH PARTITION tab5_1",
        "ALTER TABLE tab5_1 DROP COLUMN b",
        "ALTER TABLE tab5_1 ADD COLUMN b int",
        "ALTER TABLE tab5 ATTACH PARTITION tab5_1 DEFAULT",
    )
    publisher.sql("UPDATE tab5 SET a = 3 WHERE a = 2")
    publisher.wait_for_catchup("sub2")
    assert subscriber2.sql("SELECT a, b, c FROM tab5 ORDER BY 1") == (3, 1, None), (
        "updates of tab5 replicated correctly after altering table on subscriber"
    )

    # Alter the published table; replication into the partitioned target works.
    publisher.sql_batch(
        "ALTER TABLE tab5 DROP COLUMN b, ADD COLUMN c INT",
        "ALTER TABLE tab5 ADD COLUMN b INT",
    )
    publisher.sql("UPDATE tab5 SET c = 1 WHERE a = 3")
    publisher.wait_for_catchup("sub2")
    assert subscriber2.sql("SELECT a, b, c FROM tab5 ORDER BY 1") == (3, None, 1), (
        "updates of tab5 replicated correctly after altering table on publisher"
    )

    # As long as the leaf partition has the needed REPLICA IDENTITY, the target
    # partitioned table itself need not.
    subscriber2.sql("ALTER TABLE tab5 REPLICA IDENTITY NOTHING")
    publisher.sql("UPDATE tab5 SET a = 4 WHERE a = 3")
    publisher.wait_for_catchup("sub2")
    assert subscriber2.sql("SELECT a, b, c FROM tab5_1 ORDER BY 1") == (4, None, 1), (
        "updates of tab5 replicated correctly"
    )
