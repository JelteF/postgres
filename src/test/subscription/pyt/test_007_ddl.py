# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/007_ddl.pl.

Tests some logical replication DDL behaviour: disabling and dropping a
subscription in one transaction, warnings for non-existent publications on
CREATE/ALTER SUBSCRIPTION, and ALTER PUBLICATION ... RENAME during replication.
"""

import pytest

from libpq import PostgresWarning


def test_ddl(create_pg):
    publisher = create_pg("publisher", allows_streaming="logical")
    subscriber = create_pg("subscriber")
    connstr = publisher.connstr()

    ddl = "CREATE TABLE test1 (a int, b text)"
    publisher.sql(ddl)
    subscriber.sql(ddl)

    publisher.sql("CREATE PUBLICATION mypub FOR ALL TABLES")
    subscriber.sql(
        f"CREATE SUBSCRIPTION mysub CONNECTION '{connstr}' PUBLICATION mypub"
    )
    publisher.wait_for_catchup("mysub")

    # Disable and drop a subscription in one transaction must not hang.
    subscriber.sql_batch(
        "BEGIN",
        "ALTER SUBSCRIPTION mysub DISABLE",
        "ALTER SUBSCRIPTION mysub SET (slot_name = NONE)",
        "DROP SUBSCRIPTION mysub",
        "COMMIT",
    )

    # CREATE/ALTER SUBSCRIPTION warn about non-existent publications.
    with pytest.warns(
        PostgresWarning,
        match='publication "non_existent_pub" does not exist on the publisher',
    ):
        subscriber.sql(
            f"CREATE SUBSCRIPTION mysub1 CONNECTION '{connstr}' "
            "PUBLICATION mypub, non_existent_pub"
        )
    subscriber.wait_for_subscription_sync(publisher, "mysub1")

    with pytest.warns(
        PostgresWarning,
        match='publications "non_existent_pub1", "non_existent_pub2" do not exist on the publisher',
    ):
        subscriber.sql(
            "ALTER SUBSCRIPTION mysub1 ADD PUBLICATION non_existent_pub1, non_existent_pub2"
        )
    with pytest.warns(
        PostgresWarning,
        match='publication "non_existent_pub" does not exist on the publisher',
    ):
        subscriber.sql("ALTER SUBSCRIPTION mysub1 SET PUBLICATION non_existent_pub")

    publisher.sql("DROP PUBLICATION mypub")
    publisher.sql("SELECT pg_drop_replication_slot('mysub')")
    subscriber.sql("DROP SUBSCRIPTION mysub1")

    # ALTER PUBLICATION RENAME during replication.
    publisher.sql("CREATE TABLE test2 (a int, b text)")
    subscriber.sql("CREATE TABLE test2 (a int, b text)")
    publisher.sql_batch(
        "CREATE PUBLICATION pub_empty",
        "CREATE PUBLICATION pub_for_tab FOR TABLE test1",
        "CREATE PUBLICATION pub_for_all_tables FOR ALL TABLES",
    )
    subscriber.sql(
        f"CREATE SUBSCRIPTION tap_sub CONNECTION '{connstr}' PUBLICATION pub_for_tab"
    )
    subscriber.wait_for_subscription_sync(publisher, "tap_sub")

    def test_swap(table_name, pubname, appname):
        publisher.sql(f"INSERT INTO {table_name} VALUES (1)")
        publisher.wait_for_catchup(appname)
        assert subscriber.sql(f"SELECT a FROM {table_name}") == 1, (
            "check replication worked well before renaming a publication"
        )

        # Swap the names: pubname <-> pub_empty.
        publisher.sql_batch(
            f"ALTER PUBLICATION {pubname} RENAME TO tap_pub_tmp",
            f"ALTER PUBLICATION pub_empty RENAME TO {pubname}",
            "ALTER PUBLICATION tap_pub_tmp RENAME TO pub_empty",
        )
        publisher.sql(f"INSERT INTO {table_name} VALUES (2)")
        publisher.wait_for_catchup(appname)
        # The second tuple is not replicated: pubname no longer holds the table.
        assert subscriber.sql(f"SELECT a FROM {table_name} ORDER BY a") == 1, (
            "check the tuple inserted after the RENAME was not replicated"
        )

        # Restore the names.
        publisher.sql_batch(
            f"ALTER PUBLICATION {pubname} RENAME TO tap_pub_tmp",
            f"ALTER PUBLICATION pub_empty RENAME TO {pubname}",
            "ALTER PUBLICATION tap_pub_tmp RENAME TO pub_empty",
        )

    test_swap("test1", "pub_for_tab", "tap_sub")

    subscriber.sql("ALTER SUBSCRIPTION tap_sub SET PUBLICATION pub_for_all_tables")
    subscriber.wait_for_subscription_sync(publisher, "tap_sub")
    test_swap("test2", "pub_for_all_tables", "tap_sub")

    publisher.sql_batch(
        "DROP PUBLICATION pub_empty, pub_for_tab, pub_for_all_tables",
        "DROP TABLE test1, test2",
    )
    subscriber.sql("DROP SUBSCRIPTION tap_sub")
    subscriber.sql("DROP TABLE test1, test2")
