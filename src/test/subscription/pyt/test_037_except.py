# Copyright (c) 2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/037_except.pl.

Logical replication tests for publications with an EXCEPT clause: excluded
tables (plain, inherited with/without ONLY, partitioned roots) are neither
initially copied nor incrementally replicated, ALTER PUBLICATION can change the
exclusion list, and a table excluded by one publication is still replicated if
another publication includes it.
"""

import contextlib

from libpq import LibpqError


def test_except(create_pg):
    publisher = create_pg("publisher", allows_streaming="logical")
    subscriber = create_pg("subscriber")
    connstr = publisher.connstr()

    # --- non-partitioned and inherited tables --------------------------------
    publisher.sql_batch(
        "CREATE TABLE tab1 AS SELECT generate_series(1,10) AS a",
        "CREATE TABLE parent (a int)",
        "CREATE TABLE child (b int) INHERITS (parent)",
        "CREATE TABLE parent1 (a int)",
        "CREATE TABLE child1 (b int) INHERITS (parent1)",
    )
    subscriber.sql_batch(
        "CREATE TABLE tab1 (a int)",
        "CREATE TABLE parent (a int)",
        "CREATE TABLE child (b int) INHERITS (parent)",
        "CREATE TABLE parent1 (a int)",
        "CREATE TABLE child1 (b int) INHERITS (parent1)",
    )
    publisher.sql(
        "CREATE PUBLICATION tap_pub FOR ALL TABLES EXCEPT (TABLE tab1, parent, only parent1)"
    )
    publisher.sql("SELECT pg_create_logical_replication_slot('test_slot', 'pgoutput')")
    subscriber.sql(
        f"CREATE SUBSCRIPTION tap_sub CONNECTION '{connstr}' PUBLICATION tap_pub"
    )
    subscriber.wait_for_subscription_sync(publisher, "tap_sub")

    assert subscriber.sql("SELECT count(*) FROM tab1") == 0, (
        "no initial data copied for tables in the EXCEPT clause"
    )

    publisher.sql_batch(
        "INSERT INTO tab1 VALUES(generate_series(11,20))",
        "INSERT INTO child VALUES(generate_series(11,20), generate_series(11,20))",
    )
    assert (
        publisher.sql(
            "SELECT count(*) = 0 FROM pg_logical_slot_get_binary_changes('test_slot', NULL, NULL, "
            "'proto_version', '1', 'publication_names', 'tap_pub')"
        )
        is True
    ), "no changes for EXCEPT-clause tables present in the slot"

    # ONLY parent1 was excluded, so child1 is still published.
    publisher.sql(
        "INSERT INTO child1 VALUES(generate_series(11,20), generate_series(11,20))"
    )
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT count(*) FROM tab1") == 0, "check replicated inserts"
    assert subscriber.sql("SELECT count(*) FROM child") == 0, "check replicated inserts"
    assert subscriber.sql("SELECT count(*) FROM child1") == 10, (
        "check replicated inserts"
    )

    publisher.sql("CREATE TABLE tab2 AS SELECT generate_series(1,10) AS a")
    subscriber.sql("CREATE TABLE tab2 (a int)")
    publisher.sql("ALTER PUBLICATION tap_pub SET ALL TABLES EXCEPT (TABLE tab2)")
    subscriber.sql("ALTER SUBSCRIPTION tap_sub REFRESH PUBLICATION")
    subscriber.wait_for_subscription_sync(publisher, "tap_sub")
    assert subscriber.sql("SELECT count(*) FROM tab2") == 0, (
        "no initial data copied for EXCEPT-clause tables"
    )
    assert subscriber.sql("SELECT count(*) FROM tab1") == 20, (
        "data copied as tab1 was removed from EXCEPT clause"
    )

    subscriber.sql("DROP SUBSCRIPTION tap_sub")
    subscriber.sql_batch(
        "TRUNCATE TABLE tab1", "DROP TABLE parent, parent1, child, child1, tab2"
    )
    publisher.sql("DROP PUBLICATION tap_pub")
    publisher.sql_batch(
        "TRUNCATE TABLE tab1", "DROP TABLE parent, parent1, child, child1, tab2"
    )

    # --- partitioned tables --------------------------------------------------
    publisher.sql_batch(
        "CREATE TABLE root1(a int) PARTITION BY RANGE(a)",
        "CREATE TABLE part1 PARTITION OF root1 FOR VALUES FROM (0) TO (100)",
        "CREATE TABLE part2 PARTITION OF root1 FOR VALUES FROM (100) TO (200) PARTITION BY RANGE(a)",
        "CREATE TABLE part2_1 PARTITION OF part2 FOR VALUES FROM (100) TO (150)",
    )
    subscriber.sql_batch(
        "CREATE TABLE root1(a int)",
        "CREATE TABLE part1(a int)",
        "CREATE TABLE part2(a int)",
        "CREATE TABLE part2_1(a int)",
    )

    def test_except_root_partition(pubviaroot):
        # An excluded root partitioned table excludes all its partitions,
        # regardless of publish_via_partition_root.
        publisher.sql_batch(
            "CREATE PUBLICATION tap_pub_part FOR ALL TABLES EXCEPT (TABLE root1) "
            f"WITH (publish_via_partition_root = {pubviaroot})",
            "INSERT INTO root1 VALUES (1), (101)",
        )
        subscriber.sql(
            f"CREATE SUBSCRIPTION tap_sub_part CONNECTION '{connstr}' PUBLICATION tap_pub_part"
        )
        subscriber.wait_for_subscription_sync(publisher, "tap_sub_part")
        publisher.sql(
            "SELECT slot_name FROM pg_replication_slot_advance('test_slot', pg_current_wal_lsn())"
        )
        publisher.sql("INSERT INTO root1 VALUES (2), (102)")
        publisher.sql(
            "SELECT count(*) = 0 FROM pg_logical_slot_get_binary_changes('test_slot', NULL, "
            "NULL, 'proto_version', '1', 'publication_names', 'tap_pub_part')"
        )
        publisher.wait_for_catchup("tap_sub_part")
        for table in ("root1", "part1", "part2", "part2_1"):
            assert subscriber.sql(f"SELECT count(*) FROM {table}") == 0, (
                f"no rows replicated to subscriber for {table}"
            )
        subscriber.sql("DROP SUBSCRIPTION tap_sub_part")
        publisher.sql("DROP PUBLICATION tap_pub_part")

    test_except_root_partition("false")
    test_except_root_partition("true")

    # --- multiple publications -----------------------------------------------
    # Excluded by pub1's EXCEPT but included by pub2 FOR TABLE.
    publisher.sql_batch(
        "CREATE PUBLICATION tap_pub1 FOR ALL TABLES EXCEPT (TABLE tab1)",
        "CREATE PUBLICATION tap_pub2 FOR TABLE tab1",
        "INSERT INTO tab1 VALUES(1)",
    )
    subscriber.sql(
        f"CREATE SUBSCRIPTION tap_sub CONNECTION '{connstr}' PUBLICATION tap_pub1, tap_pub2"
    )
    subscriber.wait_for_subscription_sync(publisher, "tap_sub")
    publisher.sql("INSERT INTO tab1 VALUES(2)")
    publisher.wait_for_catchup("tap_sub")
    assert publisher.sql("SELECT * FROM tab1 ORDER BY a") == [1, 2], (
        "replication of a table excluded by one publication but included by another"
    )
    publisher.sql_batch("DROP PUBLICATION tap_pub2", "TRUNCATE tab1")
    subscriber.sql("TRUNCATE tab1")

    # Excluded by pub1's EXCEPT but included by pub2 FOR ALL TABLES. tap_sub
    # still exists from the previous step (the Perl test ignores the resulting
    # "already exists" error), so the recreated tap_pub2 is picked up.
    publisher.sql_batch(
        "CREATE PUBLICATION tap_pub2 FOR ALL TABLES", "INSERT INTO tab1 VALUES(1)"
    )
    with contextlib.suppress(LibpqError):
        subscriber.sql(
            f"CREATE SUBSCRIPTION tap_sub CONNECTION '{connstr}' PUBLICATION tap_pub1, tap_pub2"
        )
    subscriber.wait_for_subscription_sync(publisher, "tap_sub")
    publisher.sql("INSERT INTO tab1 VALUES(2)")
    publisher.wait_for_catchup("tap_sub")
    assert publisher.sql("SELECT * FROM tab1 ORDER BY a") == [1, 2], (
        "replication of a table excluded by one publication but included by another"
    )

    subscriber.sql("DROP SUBSCRIPTION tap_sub")
    publisher.sql("DROP PUBLICATION tap_pub1")
    publisher.sql("DROP PUBLICATION tap_pub2")
