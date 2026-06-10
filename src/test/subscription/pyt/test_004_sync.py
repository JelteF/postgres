# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/004_sync.pl.

Tests for logical replication table syncing: initial copy, recovery from a
copy blocked by a constraint violation, ALTER SUBSCRIPTION REFRESH PUBLICATION
picking up newly added tables, and that DROP SUBSCRIPTION cleans up publisher
slots and replication origins even when stuck on a failing copy.
"""


def test_sync(create_pg):
    publisher = create_pg("publisher", allows_streaming="logical")
    subscriber = create_pg("subscriber", conf={"wal_retrieve_retry_interval": "1ms"})
    connstr = publisher.connstr()

    publisher.sql("CREATE TABLE tab_rep (a int primary key)")
    publisher.sql("INSERT INTO tab_rep SELECT generate_series(1,10)")
    subscriber.sql("CREATE TABLE tab_rep (a int primary key)")

    publisher.sql("CREATE PUBLICATION tap_pub FOR ALL TABLES")
    subscriber.sql(
        f"CREATE SUBSCRIPTION tap_sub CONNECTION '{connstr}' PUBLICATION tap_pub"
    )
    subscriber.wait_for_subscription_sync(publisher, "tap_sub")
    assert subscriber.sql("SELECT count(*) FROM tab_rep") == 10, (
        "initial data synced for first sub"
    )

    # Drop the subscription to leave unreplicated data.
    subscriber.sql("DROP SUBSCRIPTION tap_sub")
    publisher.sql("INSERT INTO tab_rep SELECT generate_series(11,20)")

    # Recreate; the initial copy will get stuck on the unique constraint.
    subscriber.sql(
        f"CREATE SUBSCRIPTION tap_sub CONNECTION '{connstr}' PUBLICATION tap_pub"
    )
    started_query = "SELECT srsubstate = 'd' FROM pg_subscription_rel"
    subscriber.poll_query_until(started_query)

    # Remove the conflicting data and let sync finish.
    subscriber.sql("DELETE FROM tab_rep")
    subscriber.wait_for_subscription_sync()
    assert subscriber.sql("SELECT count(*) FROM tab_rep") == 20, (
        "initial data synced for second sub"
    )

    # Another subscription for the same node pair.
    subscriber.sql(
        f"CREATE SUBSCRIPTION tap_sub2 CONNECTION '{connstr}' PUBLICATION tap_pub "
        "WITH (copy_data = false)"
    )
    subscriber.poll_query_until(
        "SELECT pid IS NOT NULL FROM pg_stat_subscription "
        "WHERE subname = 'tap_sub2' AND worker_type = 'apply'"
    )
    subscriber.sql("DROP SUBSCRIPTION tap_sub")
    subscriber.sql("DROP SUBSCRIPTION tap_sub2")
    assert subscriber.sql("SELECT count(*) FROM pg_subscription") == 0, (
        "second and third sub are dropped"
    )

    subscriber.sql("DELETE FROM tab_rep")
    subscriber.sql(
        f"CREATE SUBSCRIPTION tap_sub CONNECTION '{connstr}' PUBLICATION tap_pub"
    )
    subscriber.wait_for_subscription_sync()
    assert subscriber.sql("SELECT count(*) FROM tab_rep") == 20, (
        "initial data synced for fourth sub"
    )

    # A table added after the subscription was initialized.
    subscriber.sql("CREATE TABLE tab_rep_next (a int)")
    publisher.sql("CREATE TABLE tab_rep_next (a) AS SELECT generate_series(1,10)")
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT count(*) FROM tab_rep_next") == 0, (
        "no data for table added after subscription initialized"
    )

    subscriber.sql("ALTER SUBSCRIPTION tap_sub REFRESH PUBLICATION")
    subscriber.wait_for_subscription_sync()
    assert subscriber.sql("SELECT count(*) FROM tab_rep_next") == 10, (
        "data for table added after subscription initialized are now synced"
    )

    publisher.sql("INSERT INTO tab_rep_next SELECT generate_series(1,10)")
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT count(*) FROM tab_rep_next") == 20, (
        "changes for table added after subscription initialized replicated"
    )

    publisher.sql("DROP TABLE tab_rep_next")
    subscriber.sql("DROP TABLE tab_rep_next")
    subscriber.sql("DROP SUBSCRIPTION tap_sub")

    # Recreating now fails the initial copy on the unique constraint; dropping
    # the subscription must still clean up publisher slots and origins.
    subscriber.sql(
        f"CREATE SUBSCRIPTION tap_sub CONNECTION '{connstr}' PUBLICATION tap_pub"
    )
    subscriber.poll_query_until(started_query)
    subscriber.sql("DROP SUBSCRIPTION tap_sub")

    publisher.poll_query_until("SELECT count(*) = 0 FROM pg_replication_slots")
    assert subscriber.sql("SELECT count(*) FROM pg_replication_origin_status") == 0, (
        "all replication origins have been cleaned up"
    )
