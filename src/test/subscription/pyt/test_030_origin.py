# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/030_origin.pl.

Tests the CREATE SUBSCRIPTION ``origin`` parameter and its interaction with
``copy_data``: a bidirectional A<->B setup with origin=none does not loop
infinitely; data that reached node_B from a third node_C is not forwarded to
node_A; origin-difference conflicts on UPDATE/DELETE are detected and logged;
and origin=none with copy_data=on warns when the publisher may hold
remotely-originated data (including via partitions). Uses a table whose name
contains a quote to exercise identifier quoting.
"""

from libpq import PostgresWarning

import pytest

TAB_UNQUOTED = "tab'le"
TAB = f'"{TAB_UNQUOTED}"'

SUBNAME_AB = "tap_sub_A_B"
SUBNAME_AB2 = "tap_sub_A_B_2"
SUBNAME_BA = "tap_sub_B_A"
SUBNAME_BC = "tap_sub_B_C"

WARN_COPY_DATA = (
    "requested copy_data with origin = NONE "
    "but might copy data that had a different origin"
)


def test_origin(create_pg):
    node_A = create_pg("node_A", allows_streaming="logical")
    # track_commit_timestamp lets node_B detect conflicts when a row was last
    # modified by a different origin.
    node_B = create_pg(
        "node_B", allows_streaming="logical", conf={"track_commit_timestamp": True}
    )

    node_A.sql(f"CREATE TABLE {TAB} (a int PRIMARY KEY)")
    node_B.sql(f"CREATE TABLE {TAB} (a int PRIMARY KEY)")

    # --- bidirectional replication A <-> B -----------------------------------
    node_A_connstr = node_A.connstr()
    node_A.sql(f"CREATE PUBLICATION tap_pub_A FOR TABLE {TAB}")
    node_B.sql(
        f"CREATE SUBSCRIPTION {SUBNAME_BA} "
        f"CONNECTION '{node_A_connstr} application_name={SUBNAME_BA}' "
        "PUBLICATION tap_pub_A WITH (origin = none)"
    )

    node_B_connstr = node_B.connstr()
    node_B.sql(f"CREATE PUBLICATION tap_pub_B FOR TABLE {TAB}")
    node_A.sql(
        f"CREATE SUBSCRIPTION {SUBNAME_AB} "
        f"CONNECTION '{node_B_connstr} application_name={SUBNAME_AB}' "
        "PUBLICATION tap_pub_B WITH (origin = none, copy_data = off)"
    )

    node_A.wait_for_subscription_sync(node_B, SUBNAME_AB)
    node_B.wait_for_subscription_sync(node_A, SUBNAME_BA)

    # --- no infinite recursion -----------------------------------------------
    node_A.sql(f"INSERT INTO {TAB} VALUES (11)")
    node_B.sql(f"INSERT INTO {TAB} VALUES (21)")
    node_A.wait_for_catchup(SUBNAME_BA)
    node_B.wait_for_catchup(SUBNAME_AB)

    assert node_A.sql(f"SELECT * FROM {TAB} ORDER BY 1") == [11, 21], (
        "Inserted successfully without leading to infinite recursion"
    )
    assert node_B.sql(f"SELECT * FROM {TAB} ORDER BY 1") == [11, 21], (
        "Inserted successfully without leading to infinite recursion"
    )

    node_A.sql(f"DELETE FROM {TAB}")
    node_A.wait_for_catchup(SUBNAME_BA)
    node_B.wait_for_catchup(SUBNAME_AB)

    # --- data from node_C reaching node_B is not forwarded to node_A ---------
    assert node_A.sql(f"SELECT * FROM {TAB} ORDER BY 1") == [], "Check existing data"
    assert node_B.sql(f"SELECT * FROM {TAB} ORDER BY 1") == [], "Check existing data"

    node_C = create_pg("node_C", allows_streaming="logical")
    node_C.sql(f"CREATE TABLE {TAB} (a int PRIMARY KEY)")

    node_C_connstr = node_C.connstr()
    node_C.sql(f"CREATE PUBLICATION tap_pub_C FOR TABLE {TAB}")
    node_B.sql(
        f"CREATE SUBSCRIPTION {SUBNAME_BC} "
        f"CONNECTION '{node_C_connstr} application_name={SUBNAME_BC}' "
        "PUBLICATION tap_pub_C WITH (origin = none)"
    )
    node_B.wait_for_subscription_sync(node_C, SUBNAME_BC)

    node_C.sql(f"INSERT INTO {TAB} VALUES (32)")
    node_C.wait_for_catchup(SUBNAME_BC)
    node_B.wait_for_catchup(SUBNAME_AB)
    node_A.wait_for_catchup(SUBNAME_BA)

    assert node_B.sql(f"SELECT * FROM {TAB} ORDER BY 1") == 32, (
        "The node_C data replicated to node_B"
    )
    assert node_A.sql(f"SELECT * FROM {TAB} ORDER BY 1") == [], (
        "Remote data originating from another node is not replicated with origin none"
    )

    # --- origin-difference conflicts on UPDATE/DELETE ------------------------
    node_B.sql(f"DELETE FROM {TAB}")
    node_A.sql(f"INSERT INTO {TAB} VALUES (32)")
    node_A.wait_for_catchup(SUBNAME_BA)
    node_B.wait_for_catchup(SUBNAME_AB)
    assert node_B.sql(f"SELECT * FROM {TAB} ORDER BY 1") == 32, (
        "The node_A data replicated to node_B"
    )

    # node_C updates the row that node_A inserted: update_origin_differs.
    offset = node_B.current_log_position()
    node_C.sql(f"UPDATE {TAB} SET a = 33 WHERE a = 32")
    node_B.wait_for_log(
        rf'conflict detected on relation "public.{TAB_UNQUOTED}": '
        r"conflict=update_origin_differs.*\n.*DETAIL:.* Updating the row that was "
        r'modified by a different origin ".*" in transaction [0-9]+ at .*: '
        r"local row \(32\), remote row \(33\), replica identity \(a\)=\(32\)\.",
        offset,
    )

    node_B.sql(f"DELETE FROM {TAB}")
    node_A.sql(f"INSERT INTO {TAB} VALUES (33)")
    node_A.wait_for_catchup(SUBNAME_BA)
    node_B.wait_for_catchup(SUBNAME_AB)
    assert node_B.sql(f"SELECT * FROM {TAB} ORDER BY 1") == 33, (
        "The node_A data replicated to node_B"
    )

    # node_C deletes the row that node_A inserted: delete_origin_differs.
    offset = node_B.current_log_position()
    node_C.sql(f"DELETE FROM {TAB} WHERE a = 33")
    node_B.wait_for_log(
        rf'conflict detected on relation "public.{TAB_UNQUOTED}": '
        r"conflict=delete_origin_differs.*\n.*DETAIL:.* Deleting the row that was "
        r'modified by a different origin ".*" in transaction [0-9]+ at .*: '
        r"local row \(33\), replica identity \(a\)=\(33\).*",
        offset,
    )

    # The remaining tests no longer test conflict detection.
    node_B.append_conf(track_commit_timestamp=False)
    node_B.pg_ctl("restart")

    # --- origin=none + copy_data=on warns about possible remote data ---------
    with pytest.warns(PostgresWarning, match=WARN_COPY_DATA):
        node_A.sql(
            f"CREATE SUBSCRIPTION {SUBNAME_AB2} "
            f"CONNECTION '{node_B_connstr} application_name={SUBNAME_AB2}' "
            "PUBLICATION tap_pub_B WITH (origin = none, copy_data = on)"
        )
    node_A.wait_for_subscription_sync(node_B, SUBNAME_AB2)

    # REFRESH PUBLICATION is fine when no new table is added.
    node_A.sql(f"ALTER SUBSCRIPTION {SUBNAME_AB2} REFRESH PUBLICATION")

    # Add a table on both nodes that subscribes from a different publication.
    node_A.sql("CREATE TABLE tab_new (a int PRIMARY KEY)")
    node_B.sql("CREATE TABLE tab_new (a int PRIMARY KEY)")
    node_A.sql("ALTER PUBLICATION tap_pub_A ADD TABLE tab_new")
    node_B.sql(f"ALTER SUBSCRIPTION {SUBNAME_BA} REFRESH PUBLICATION")
    node_B.wait_for_subscription_sync(node_A, SUBNAME_BA)

    node_B.sql("ALTER PUBLICATION tap_pub_B ADD TABLE tab_new")
    # REFRESH now warns: the new table on the publisher subscribes elsewhere.
    with pytest.warns(PostgresWarning, match=WARN_COPY_DATA):
        node_A.sql(f"ALTER SUBSCRIPTION {SUBNAME_AB2} REFRESH PUBLICATION")

    node_A.poll_query_until(
        "SELECT count(1) = 0 FROM pg_subscription_rel WHERE srsubstate NOT IN ('r')"
    )
    node_B.wait_for_catchup(SUBNAME_AB2)

    node_A.sql("DROP TABLE tab_new")
    node_A.sql(f"DROP SUBSCRIPTION {SUBNAME_AB2}")
    node_A.sql(f"DROP SUBSCRIPTION {SUBNAME_AB}")
    node_A.sql("DROP PUBLICATION tap_pub_A")
    node_B.sql("DROP TABLE tab_new")
    node_B.sql(f"DROP SUBSCRIPTION {SUBNAME_BA}")
    node_B.sql("DROP PUBLICATION tap_pub_B")

    # --- origin=none + copy_data=on on a partitioned table warns -------------
    # node_A holds a table that becomes the source for a partition on node_B.
    node_A.sql_batch(
        "CREATE TABLE tab_part2(a int)",
        "CREATE PUBLICATION tap_pub_A FOR TABLE tab_part2",
    )
    node_B.sql_batch(
        "CREATE TABLE tab_main(a int) PARTITION BY RANGE(a)",
        "CREATE TABLE tab_part1 PARTITION OF tab_main FOR VALUES FROM (0) TO (5)",
        "CREATE TABLE tab_part2(a int) PARTITION BY RANGE(a)",
        "CREATE TABLE tab_part2_1 PARTITION OF tab_part2 FOR VALUES FROM (5) TO (10)",
        "ALTER TABLE tab_main ATTACH PARTITION tab_part2 FOR VALUES FROM (5) to (10)",
    )
    node_B.sql(
        f"CREATE SUBSCRIPTION tap_sub_A_B CONNECTION '{node_A_connstr}' PUBLICATION tap_pub_A"
    )
    node_C.sql_batch(
        "CREATE TABLE tab_main(a int)",
        "CREATE TABLE tab_part2_1(a int)",
    )
    node_B.sql_batch(
        "CREATE PUBLICATION tap_pub_B FOR TABLE tab_main WITH (publish_via_partition_root)",
        "CREATE PUBLICATION tap_pub_B_2 FOR TABLE tab_part2_1",
    )

    # Partition tab_part2 on node_B subscribes from node_A, so it may hold
    # remotely-originated data.
    with pytest.warns(PostgresWarning, match=WARN_COPY_DATA):
        node_C.sql(
            f"CREATE SUBSCRIPTION tap_sub_B_C CONNECTION '{node_B_connstr}' "
            "PUBLICATION tap_pub_B WITH (origin = none, copy_data = on)"
        )
    node_C.sql("DROP SUBSCRIPTION tap_sub_B_C")

    # The ancestor of tab_part2_1 on node_B subscribes from node_A likewise.
    with pytest.warns(PostgresWarning, match=WARN_COPY_DATA):
        node_C.sql(
            f"CREATE SUBSCRIPTION tap_sub_B_C CONNECTION '{node_B_connstr}' "
            "PUBLICATION tap_pub_B_2 WITH (origin = none, copy_data = on)"
        )

    node_C.sql("DROP SUBSCRIPTION tap_sub_B_C")
    node_C.sql_batch("DROP TABLE tab_main", "DROP TABLE tab_part2_1")
    node_B.sql("DROP SUBSCRIPTION tap_sub_A_B")
    node_B.sql_batch(
        "DROP PUBLICATION tap_pub_B",
        "DROP PUBLICATION tap_pub_B_2",
        "DROP TABLE tab_main",
    )
    node_A.sql_batch(
        "DROP PUBLICATION tap_pub_A",
        "DROP TABLE tab_part2",
    )
