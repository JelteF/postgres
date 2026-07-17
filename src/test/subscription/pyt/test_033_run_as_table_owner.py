# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/033_run_as_table_owner.pl.

Test that logical replication respects permissions: with run_as_owner=true the
apply worker runs as the subscription owner (so it needs table privileges or
INHERIT of the owner role, not just SET ROLE), and with run_as_owner=false it
runs as the table owner.
"""


def test_run_as_table_owner(create_pg):
    publisher = create_pg("publisher", allows_streaming="logical")
    subscriber = create_pg("subscriber")
    connstr = publisher.connstr()

    offset = 0
    perm_denied = r"ERROR: ( [A-Z0-9]+:)? permission denied for table unpartitioned"

    def publish_insert(tbl, new_i):
        publisher.sql_batch_oneshot(
            "SET SESSION AUTHORIZATION regress_alice",
            f"INSERT INTO {tbl} (i) VALUES ({new_i})",
        )

    def publish_update(tbl, old_i, new_i):
        publisher.sql_batch_oneshot(
            "SET SESSION AUTHORIZATION regress_alice",
            f"UPDATE {tbl} SET i = {new_i} WHERE i = {old_i}",
        )

    def publish_delete(tbl, old_i):
        publisher.sql_batch_oneshot(
            "SET SESSION AUTHORIZATION regress_alice",
            f"DELETE FROM {tbl} WHERE i = {old_i}",
        )

    def expect_replication(tbl, cnt, mn, mx, name):
        publisher.wait_for_catchup("admin_sub")
        assert subscriber.sql(f"SELECT COUNT(i), MIN(i), MAX(i) FROM {tbl}") == (
            cnt,
            mn,
            mx,
        ), name

    def expect_failure(tbl, cnt, mn, mx, name):
        nonlocal offset
        offset = subscriber.wait_for_log(perm_denied, offset)
        assert subscriber.sql(f"SELECT COUNT(i), MIN(i), MAX(i) FROM {tbl}") == (
            cnt,
            mn,
            mx,
        ), name

    for node in (publisher, subscriber):
        node.sql_batch_oneshot(
            "CREATE ROLE regress_admin SUPERUSER LOGIN",
            "CREATE ROLE regress_admin2 SUPERUSER LOGIN",
            "CREATE ROLE regress_alice NOSUPERUSER LOGIN",
            "GRANT CREATE ON DATABASE postgres TO regress_alice",
            "SET SESSION AUTHORIZATION regress_alice",
            "CREATE SCHEMA alice",
            "GRANT USAGE ON SCHEMA alice TO regress_admin",
            "CREATE TABLE alice.unpartitioned (i INTEGER)",
            "ALTER TABLE alice.unpartitioned REPLICA IDENTITY FULL",
            "GRANT SELECT ON TABLE alice.unpartitioned TO regress_admin",
        )
    publisher.sql_batch_oneshot(
        "SET SESSION AUTHORIZATION regress_alice",
        "CREATE PUBLICATION alice FOR TABLE alice.unpartitioned "
        "WITH (publish_via_partition_root = true)",
    )
    with subscriber.connect(user="regress_admin") as c:
        c.sql(
            f"CREATE SUBSCRIPTION admin_sub CONNECTION '{connstr}' PUBLICATION alice "
            "WITH (run_as_owner = true, password_required = false)"
        )
    subscriber.wait_for_subscription_sync(publisher, "admin_sub")

    # A superuser owner can replicate.
    publish_insert("alice.unpartitioned", 1)
    publish_insert("alice.unpartitioned", 3)
    publish_insert("alice.unpartitioned", 5)
    publish_update("alice.unpartitioned", 1, 7)
    publish_delete("alice.unpartitioned", 3)
    expect_replication("alice.unpartitioned", 2, 5, 7, "superuser can replicate")

    # Without superuser and without table privileges, apply fails.
    subscriber.sql("ALTER ROLE regress_admin NOSUPERUSER")
    publish_insert("alice.unpartitioned", 9)
    expect_failure(
        "alice.unpartitioned", 2, 5, 7, "with no privileges cannot replicate"
    )

    # INSERT privilege lets an INSERT replicate.
    subscriber.sql_batch_oneshot(
        "ALTER ROLE regress_admin NOSUPERUSER",
        "SET SESSION AUTHORIZATION regress_alice",
        "GRANT INSERT,UPDATE,DELETE ON alice.unpartitioned TO regress_admin",
        "REVOKE SELECT ON alice.unpartitioned FROM regress_admin",
    )
    expect_replication(
        "alice.unpartitioned", 3, 5, 9, "with INSERT privilege can replicate INSERT"
    )

    # Without SELECT, UPDATE/DELETE can't replicate.
    publish_update("alice.unpartitioned", 5, 11)
    publish_delete("alice.unpartitioned", 9)
    expect_failure(
        "alice.unpartitioned",
        3,
        5,
        9,
        "without SELECT privilege cannot replicate UPDATE/DELETE",
    )

    # With SELECT, replication resumes.
    subscriber.sql_batch_oneshot(
        "SET SESSION AUTHORIZATION regress_alice",
        "GRANT SELECT ON alice.unpartitioned TO regress_admin",
    )
    expect_replication(
        "alice.unpartitioned", 2, 7, 11, "with all privileges can replicate"
    )

    # SET ROLE without INHERIT doesn't grant table privileges under run_as_owner.
    subscriber.sql_batch_oneshot(
        "SET SESSION AUTHORIZATION regress_alice",
        "REVOKE ALL PRIVILEGES ON alice.unpartitioned FROM regress_admin",
        "RESET SESSION AUTHORIZATION",
        "GRANT regress_alice TO regress_admin WITH INHERIT FALSE, SET TRUE",
    )
    publish_insert("alice.unpartitioned", 13)
    expect_failure(
        "alice.unpartitioned",
        2,
        7,
        11,
        "with SET ROLE but not INHERIT cannot replicate",
    )

    # INHERIT (not SET ROLE) works.
    subscriber.sql("GRANT regress_alice TO regress_admin WITH INHERIT TRUE, SET FALSE")
    expect_replication(
        "alice.unpartitioned", 3, 7, 13, "with INHERIT but not SET ROLE can replicate"
    )

    subscriber.sql_batch_oneshot(
        "SET SESSION AUTHORIZATION regress_alice",
        "REVOKE ALL PRIVILEGES ON alice.unpartitioned FROM regress_admin",
        "RESET SESSION AUTHORIZATION",
        "GRANT regress_alice TO regress_admin WITH INHERIT FALSE, SET TRUE",
    )
    publish_insert("alice.unpartitioned", 14)
    expect_failure(
        "alice.unpartitioned", 3, 7, 13, "with no privileges cannot replicate"
    )

    # run_as_owner = false runs apply as the table owner.
    subscriber.sql("ALTER SUBSCRIPTION admin_sub SET (run_as_owner = false)")
    expect_replication(
        "alice.unpartitioned",
        4,
        7,
        14,
        "can replicate after setting run_as_owner to false",
    )

    subscriber.sql("DROP SUBSCRIPTION admin_sub")
    subscriber.sql("TRUNCATE alice.unpartitioned")

    # A new subscription owned by regress_admin2 with run_as_owner=false; the
    # initial copy runs as the table owner, so all data is copied.
    with subscriber.connect(user="regress_admin2") as c:
        c.sql(
            f"CREATE SUBSCRIPTION admin_sub CONNECTION '{connstr}' PUBLICATION alice "
            "WITH (run_as_owner = false, password_required = false, copy_data = true, "
            "enabled = false)"
        )
    subscriber.sql("ALTER ROLE regress_admin2 NOSUPERUSER")
    subscriber.sql("GRANT regress_alice TO regress_admin2 WITH INHERIT FALSE, SET TRUE")
    subscriber.sql("ALTER SUBSCRIPTION admin_sub ENABLE")
    subscriber.wait_for_subscription_sync(publisher, "admin_sub")
    expect_replication(
        "alice.unpartitioned", 4, 7, 14, "table owner can do the initial data copy"
    )
