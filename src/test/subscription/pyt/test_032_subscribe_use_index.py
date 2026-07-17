# Copyright (c) 2022-2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/032_subscribe_use_index.pl.

Tests that a subscriber applying changes for a REPLICA IDENTITY FULL table
picks a usable index to find the tuple to update/delete instead of a
sequential scan: multi-column indexes, indexes on partitioned tables,
expression+column indexes, unique indexes, and hash indexes are used, while
expression-only and partial indexes are not. idx_scan counters in
pg_stat_all_indexes confirm which path was taken.
"""


def test_subscribe_use_index(create_pg):
    publisher = create_pg("publisher", allows_streaming="logical")
    subscriber = create_pg("subscriber")
    connstr = f"{publisher.connstr()} application_name=tap_sub"

    # =====================================================================
    # Subscription can use an index with multiple rows and columns.
    publisher.sql("CREATE TABLE test_replica_id_full (x int, y text)")
    publisher.sql("ALTER TABLE test_replica_id_full REPLICA IDENTITY FULL")
    subscriber.sql("CREATE TABLE test_replica_id_full (x int, y text)")
    subscriber.sql("CREATE INDEX test_replica_id_full_idx ON test_replica_id_full(x,y)")

    publisher.sql(
        "INSERT INTO test_replica_id_full SELECT (i%10), (i%10)::text FROM generate_series(0,10) i"
    )
    publisher.sql("CREATE PUBLICATION tap_pub_rep_full FOR TABLE test_replica_id_full")
    subscriber.sql(
        f"CREATE SUBSCRIPTION tap_sub_rep_full CONNECTION '{connstr}' "
        "PUBLICATION tap_pub_rep_full"
    )
    subscriber.wait_for_subscription_sync(publisher, "tap_sub")

    publisher.sql("DELETE FROM test_replica_id_full WHERE x IN (5, 6)")
    publisher.sql(
        "UPDATE test_replica_id_full SET x = 100, y = '200' WHERE x IN (1, 2)"
    )

    publisher.wait_for_catchup("tap_sub")
    subscriber.poll_query_until(
        "select (idx_scan = 4) from pg_stat_all_indexes "
        "where indexrelname = 'test_replica_id_full_idx'"
    )

    assert (
        subscriber.sql(
            "select count(*) from test_replica_id_full WHERE (x = 100 and y = '200')"
        )
        == 2
    ), "ensure subscriber has the correct data at the end of the test"
    assert (
        subscriber.sql("select count(*) from test_replica_id_full where x in (5, 6)")
        == 0
    ), "ensure subscriber has the correct data at the end of the test"

    publisher.sql("DROP PUBLICATION tap_pub_rep_full")
    publisher.sql("DROP TABLE test_replica_id_full")
    subscriber.sql("DROP SUBSCRIPTION tap_sub_rep_full")
    subscriber.sql("DROP TABLE test_replica_id_full")

    # =====================================================================
    # Subscription can use an index on partitioned tables.
    publisher.sql_batch(
        "CREATE TABLE users_table_part(user_id bigint, value_1 int, value_2 int) PARTITION BY RANGE (value_1)",
        "CREATE TABLE users_table_part_0 PARTITION OF users_table_part FOR VALUES FROM (0) TO (10)",
        "CREATE TABLE users_table_part_1 PARTITION OF users_table_part FOR VALUES FROM (10) TO (20)",
        "ALTER TABLE users_table_part REPLICA IDENTITY FULL",
        "ALTER TABLE users_table_part_0 REPLICA IDENTITY FULL",
        "ALTER TABLE users_table_part_1 REPLICA IDENTITY FULL",
    )
    subscriber.sql_batch(
        "CREATE TABLE users_table_part(user_id bigint, value_1 int, value_2 int) PARTITION BY RANGE (value_1)",
        "CREATE TABLE users_table_part_0 PARTITION OF users_table_part FOR VALUES FROM (0) TO (10)",
        "CREATE TABLE users_table_part_1 PARTITION OF users_table_part FOR VALUES FROM (10) TO (20)",
        "CREATE INDEX users_table_part_idx ON users_table_part(user_id, value_1)",
    )
    publisher.sql(
        "INSERT INTO users_table_part SELECT (i%100), (i%20), i FROM generate_series(0,100) i"
    )
    publisher.sql("CREATE PUBLICATION tap_pub_rep_full FOR TABLE users_table_part")
    subscriber.sql(
        f"CREATE SUBSCRIPTION tap_sub_rep_full CONNECTION '{connstr}' "
        "PUBLICATION tap_pub_rep_full"
    )
    subscriber.wait_for_subscription_sync(publisher, "tap_sub")

    publisher.sql("UPDATE users_table_part SET value_1 = 0 WHERE user_id = 4")
    publisher.sql("DELETE FROM users_table_part WHERE user_id = 1 and value_1 = 1")
    publisher.sql("DELETE FROM users_table_part WHERE user_id = 12 and value_1 = 12")

    publisher.wait_for_catchup("tap_sub")
    subscriber.poll_query_until(
        "select sum(idx_scan)=3 from pg_stat_all_indexes "
        "where indexrelname ilike 'users_table_part_%'"
    )

    assert (
        subscriber.sql("select sum(user_id+value_1+value_2) from users_table_part")
        == 10907
    ), "ensure subscriber has the correct data at the end of the test"
    assert (
        subscriber.sql(
            "select count(DISTINCT(user_id,value_1, value_2)) from users_table_part"
        )
        == 99
    ), "ensure subscriber has the correct data at the end of the test"

    publisher.sql("DROP PUBLICATION tap_pub_rep_full")
    publisher.sql("DROP TABLE users_table_part")
    subscriber.sql("DROP SUBSCRIPTION tap_sub_rep_full")
    subscriber.sql("DROP TABLE users_table_part")

    # =====================================================================
    # Subscription will not use expression-only or partial indexes.
    publisher.sql("CREATE TABLE people (firstname text, lastname text)")
    publisher.sql("ALTER TABLE people REPLICA IDENTITY FULL")
    subscriber.sql("CREATE TABLE people (firstname text, lastname text)")
    subscriber.sql(
        "CREATE INDEX people_names_expr_only ON people ((firstname || ' ' || lastname))"
    )
    subscriber.sql(
        "CREATE INDEX people_names_partial ON people(firstname) "
        "WHERE (firstname = 'first_name_1')"
    )
    publisher.sql(
        "INSERT INTO people SELECT 'first_name_' || i::text, 'last_name_' || i::text "
        "FROM generate_series(0,200) i"
    )
    publisher.sql("CREATE PUBLICATION tap_pub_rep_full FOR TABLE people")
    subscriber.sql(
        f"CREATE SUBSCRIPTION tap_sub_rep_full CONNECTION '{connstr}' "
        "PUBLICATION tap_pub_rep_full"
    )
    subscriber.wait_for_subscription_sync(publisher, "tap_sub")

    publisher.sql(
        "UPDATE people SET firstname = 'no-name' WHERE firstname = 'first_name_1'"
    )
    publisher.sql(
        "UPDATE people SET firstname = 'no-name' "
        "WHERE firstname = 'first_name_2' AND lastname = 'last_name_2'"
    )
    publisher.wait_for_catchup("tap_sub")
    assert (
        subscriber.sql(
            "select sum(idx_scan) from pg_stat_all_indexes "
            "where indexrelname IN ('people_names_expr_only', 'people_names_partial')"
        )
        == 0
    ), (
        "ensure subscriber tap_sub_rep_full updates two rows via seq. scan "
        "with index on expressions"
    )

    publisher.sql("DELETE FROM people WHERE firstname = 'first_name_3'")
    publisher.sql(
        "DELETE FROM people WHERE firstname = 'first_name_4' AND lastname = 'last_name_4'"
    )
    publisher.wait_for_catchup("tap_sub")
    assert (
        subscriber.sql(
            "select sum(idx_scan) from pg_stat_all_indexes "
            "where indexrelname IN ('people_names_expr_only', 'people_names_partial')"
        )
        == 0
    ), (
        "ensure subscriber tap_sub_rep_full updates two rows via seq. scan "
        "with index on expressions"
    )
    assert subscriber.sql("SELECT count(*) FROM people") == 199, (
        "ensure subscriber has the correct data at the end of the test"
    )

    publisher.sql("DROP PUBLICATION tap_pub_rep_full")
    publisher.sql("DROP TABLE people")
    subscriber.sql("DROP SUBSCRIPTION tap_sub_rep_full")
    subscriber.sql("DROP TABLE people")

    # =====================================================================
    # Subscription can use an index having both expressions and columns.
    publisher.sql("CREATE TABLE people (firstname text, lastname text)")
    publisher.sql("ALTER TABLE people REPLICA IDENTITY FULL")
    subscriber.sql("CREATE TABLE people (firstname text, lastname text)")
    subscriber.sql(
        "CREATE INDEX people_names ON people (firstname, lastname, (firstname || ' ' || lastname))"
    )
    publisher.sql(
        "INSERT INTO people SELECT 'first_name_' || i::text, 'last_name_' || i::text "
        "FROM generate_series(0, 20) i"
    )
    publisher.sql("CREATE PUBLICATION tap_pub_rep_full FOR TABLE people")
    subscriber.sql(
        f"CREATE SUBSCRIPTION tap_sub_rep_full CONNECTION '{connstr}' "
        "PUBLICATION tap_pub_rep_full"
    )
    subscriber.wait_for_subscription_sync(publisher, "tap_sub")

    publisher.sql(
        "UPDATE people SET firstname = 'no-name' WHERE firstname = 'first_name_1'"
    )
    publisher.sql("DELETE FROM people WHERE firstname = 'no-name'")
    publisher.wait_for_catchup("tap_sub")
    subscriber.poll_query_until(
        "select idx_scan=2 from pg_stat_all_indexes where indexrelname = 'people_names'"
    )

    assert subscriber.sql("SELECT count(*) FROM people") == 20, (
        "ensure subscriber has the correct data at the end of the test"
    )
    assert (
        subscriber.sql("SELECT count(*) FROM people WHERE firstname = 'no-name'") == 0
    ), "ensure subscriber has the correct data at the end of the test"

    # Drop the index with the expression; fall back to sequential scan.
    subscriber.sql("DROP INDEX people_names")
    publisher.sql("DELETE FROM people WHERE lastname = 'last_name_18'")
    publisher.wait_for_catchup("tap_sub")
    assert (
        subscriber.sql("SELECT count(*) FROM people WHERE lastname = 'last_name_18'")
        == 0
    ), "ensure subscriber has the correct data at the end of the test"

    publisher.sql("DROP PUBLICATION tap_pub_rep_full")
    publisher.sql("DROP TABLE people")
    subscriber.sql("DROP SUBSCRIPTION tap_sub_rep_full")
    subscriber.sql("DROP TABLE people")

    # =====================================================================
    # Null values and a missing column.
    publisher.sql("CREATE TABLE test_replica_id_full (x int)")
    publisher.sql("ALTER TABLE test_replica_id_full REPLICA IDENTITY FULL")
    subscriber.sql("CREATE TABLE test_replica_id_full (x int, y int)")
    subscriber.sql("CREATE INDEX test_replica_id_full_idx ON test_replica_id_full(x,y)")
    publisher.sql("CREATE PUBLICATION tap_pub_rep_full FOR TABLE test_replica_id_full")
    subscriber.sql(
        f"CREATE SUBSCRIPTION tap_sub_rep_full CONNECTION '{connstr}' "
        "PUBLICATION tap_pub_rep_full"
    )
    subscriber.wait_for_subscription_sync(publisher, "tap_sub")

    publisher.sql("INSERT INTO test_replica_id_full VALUES (1), (2), (3)")
    publisher.sql("UPDATE test_replica_id_full SET x = x + 1 WHERE x = 1")
    publisher.wait_for_catchup("tap_sub")
    subscriber.poll_query_until(
        "select idx_scan=1 from pg_stat_all_indexes "
        "where indexrelname = 'test_replica_id_full_idx'"
    )

    assert (
        subscriber.sql("select sum(x) from test_replica_id_full WHERE y IS NULL") == 7
    ), "ensure subscriber has the correct data at the end of the test"
    assert (
        subscriber.sql("select count(*) from test_replica_id_full WHERE y IS NULL") == 3
    ), "ensure subscriber has the correct data at the end of the test"

    publisher.sql("DROP PUBLICATION tap_pub_rep_full")
    publisher.sql("DROP TABLE test_replica_id_full")
    subscriber.sql("DROP SUBSCRIPTION tap_sub_rep_full")
    subscriber.sql("DROP TABLE test_replica_id_full")

    # =====================================================================
    # Unique index when pub/sub have different data: only 1 row updated.
    publisher.sql("CREATE TABLE test_replica_id_full (x int, y int)")
    publisher.sql("ALTER TABLE test_replica_id_full REPLICA IDENTITY FULL")
    subscriber.sql("CREATE TABLE test_replica_id_full (x int, y int)")
    subscriber.sql(
        "CREATE UNIQUE INDEX test_replica_id_full_idxy ON test_replica_id_full(x,y)"
    )
    publisher.sql(
        "INSERT INTO test_replica_id_full SELECT i, i FROM generate_series(0,21) i"
    )
    publisher.sql("CREATE PUBLICATION tap_pub_rep_full FOR TABLE test_replica_id_full")
    subscriber.sql(
        f"CREATE SUBSCRIPTION tap_sub_rep_full CONNECTION '{connstr}' "
        "PUBLICATION tap_pub_rep_full"
    )
    subscriber.wait_for_subscription_sync(publisher, "tap_sub")

    # Subscriber has extra duplicates for the y column.
    subscriber.sql(
        "INSERT INTO test_replica_id_full SELECT i+100, i FROM generate_series(0,21) i"
    )
    # Update only 1 row on the publisher; the subscriber updates exactly 1 even
    # though two tuples have y = 15.
    publisher.sql("UPDATE test_replica_id_full SET x = 2000 WHERE y = 15")
    publisher.wait_for_catchup("tap_sub")
    subscriber.poll_query_until(
        "select (idx_scan = 1) from pg_stat_all_indexes "
        "where indexrelname = 'test_replica_id_full_idxy'"
    )

    assert (
        subscriber.sql("SELECT count(*) FROM test_replica_id_full WHERE x = 2000") == 1
    ), "ensure subscriber has the correct data at the end of the test"

    publisher.sql("DROP PUBLICATION tap_pub_rep_full")
    publisher.sql("DROP TABLE test_replica_id_full")
    subscriber.sql("DROP SUBSCRIPTION tap_sub_rep_full")
    subscriber.sql("DROP TABLE test_replica_id_full")

    # =====================================================================
    # Subscription can use a hash index.
    publisher.sql("CREATE TABLE test_replica_id_full (x int, y text)")
    publisher.sql("ALTER TABLE test_replica_id_full REPLICA IDENTITY FULL")
    subscriber.sql("CREATE TABLE test_replica_id_full (x int, y text)")
    subscriber.sql(
        "CREATE INDEX test_replica_id_full_idx ON test_replica_id_full USING HASH (x)"
    )
    publisher.sql(
        "INSERT INTO test_replica_id_full SELECT i, (i%10)::text FROM generate_series(0,10) i"
    )
    publisher.sql("CREATE PUBLICATION tap_pub_rep_full FOR TABLE test_replica_id_full")
    subscriber.sql(
        f"CREATE SUBSCRIPTION tap_sub_rep_full CONNECTION '{connstr}' "
        "PUBLICATION tap_pub_rep_full"
    )
    subscriber.wait_for_subscription_sync(publisher, "tap_sub")

    publisher.sql("DELETE FROM test_replica_id_full WHERE x IN (5, 6)")
    publisher.sql(
        "UPDATE test_replica_id_full SET x = 100, y = '200' WHERE x IN (1, 2)"
    )
    publisher.wait_for_catchup("tap_sub")
    subscriber.poll_query_until(
        "select (idx_scan = 4) from pg_stat_all_indexes "
        "where indexrelname = 'test_replica_id_full_idx'"
    )

    assert (
        subscriber.sql(
            "select count(*) from test_replica_id_full WHERE (x = 100 and y = '200')"
        )
        == 2
    ), "ensure subscriber has the correct data at the end of the test"
    assert (
        subscriber.sql("select count(*) from test_replica_id_full where x in (5, 6)")
        == 0
    ), "ensure subscriber has the correct data at the end of the test"

    publisher.sql("DROP PUBLICATION tap_pub_rep_full")
    publisher.sql("DROP TABLE test_replica_id_full")
    subscriber.sql("DROP SUBSCRIPTION tap_sub_rep_full")
    subscriber.sql("DROP TABLE test_replica_id_full")
