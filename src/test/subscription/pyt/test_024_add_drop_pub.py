# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/024_add_drop_pub.pl.

Test ALTER SUBSCRIPTION ... ADD/DROP/SET PUBLICATION, and that pointing a
subscription at a not-yet-existing publication only logs a warning and resumes
replication once the publication is created.
"""


def test_add_drop_pub(create_pg):
    publisher = create_pg("publisher", allows_streaming="logical")
    subscriber = create_pg("subscriber")
    connstr = publisher.connstr()

    publisher.sql("CREATE TABLE tab_1 (a int)")
    publisher.sql("INSERT INTO tab_1 SELECT generate_series(1,10)")
    subscriber.sql("CREATE TABLE tab_1 (a int)")

    publisher.sql("CREATE PUBLICATION tap_pub_1 FOR TABLE tab_1")
    publisher.sql("CREATE PUBLICATION tap_pub_2")
    subscriber.sql(
        f"CREATE SUBSCRIPTION tap_sub CONNECTION '{connstr}' "
        "PUBLICATION tap_pub_1, tap_pub_2"
    )
    subscriber.wait_for_subscription_sync(publisher, "tap_sub")
    assert subscriber.sql("SELECT count(*), min(a), max(a) FROM tab_1") == (
        10,
        1,
        10,
    ), "check initial data is copied to subscriber"

    publisher.sql("CREATE TABLE tab_2 (a int)")
    publisher.sql("INSERT INTO tab_2 SELECT generate_series(1,10)")
    subscriber.sql("CREATE TABLE tab_2 (a int)")
    publisher.sql("ALTER PUBLICATION tap_pub_2 ADD TABLE tab_2")

    # Dropping tap_pub_1 refreshes the whole publication list.
    subscriber.sql("ALTER SUBSCRIPTION tap_sub DROP PUBLICATION tap_pub_1")
    subscriber.wait_for_subscription_sync(publisher, "tap_sub")
    assert subscriber.sql("SELECT count(*), min(a), max(a) FROM tab_2") == (
        10,
        1,
        10,
    ), "check initial data is copied to subscriber"

    # Re-adding tap_pub_1 refreshes again.
    subscriber.sql("ALTER SUBSCRIPTION tap_sub ADD PUBLICATION tap_pub_1")
    subscriber.wait_for_subscription_sync(publisher, "tap_sub")
    assert subscriber.sql("SELECT count(*), min(a), max(a) FROM tab_1") == (
        20,
        1,
        10,
    ), "check initial data is copied to subscriber"

    # Setting a missing publication only warns and lets replication continue,
    # resuming once the publication is created.
    publisher.sql("CREATE TABLE tab_3 (a int)")
    subscriber.sql("CREATE TABLE tab_3 (a int)")
    oldpid = publisher.sql(
        "SELECT pid FROM pg_stat_replication "
        "WHERE application_name = 'tap_sub' AND state = 'streaming'"
    )
    subscriber.sql("ALTER SUBSCRIPTION tap_sub SET PUBLICATION tap_pub_3")
    publisher.poll_query_until(
        f"SELECT pid != {oldpid} FROM pg_stat_replication "
        "WHERE application_name = 'tap_sub' AND state = 'streaming'"
    )

    offset = publisher.current_log_position()
    publisher.sql("INSERT INTO tab_3 values(1)")
    publisher.wait_for_log(
        r'WARNING: ( [A-Z0-9]+:)? skipped loading publication "tap_pub_3"', offset
    )

    publisher.sql("CREATE PUBLICATION tap_pub_3 FOR TABLE tab_3")
    subscriber.sql("ALTER SUBSCRIPTION tap_sub REFRESH PUBLICATION")
    subscriber.wait_for_subscription_sync(publisher, "tap_sub")

    publisher.sql("INSERT INTO tab_3 values(2)")
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT * FROM tab_3 ORDER BY a") == [1, 2], (
        "incremental data replicated after the publication is created"
    )
