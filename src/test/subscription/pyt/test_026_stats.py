# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/026_stats.pl.

Tests pg_stat_subscription_stats: tablesync, sequencesync and apply errors,
plus insert_exists/delete_missing conflict counters all get bumped, can be
reset per-subscription and globally (resetting bumps stats_reset), and the
stats entry disappears when the subscription is dropped.
"""


def test_stats(create_pg):
    publisher = create_pg("publisher", allows_streaming="logical")
    subscriber = create_pg("subscriber")

    def create_sub_pub_w_errors(table_name, sequence_name):
        """Set up a publication/subscription pair that deliberately produces
        sync and apply errors and conflicts, then clears them so replication
        can proceed. Returns the subscription name."""
        # Subscriber's table has a primary key and pre-populated row that will
        # conflict with replicated data; the sequence uses a different INCREMENT
        # so sequencesync fails too.
        publisher.sql_batch(
            "BEGIN",
            f"CREATE TABLE {table_name}(a int)",
            f"ALTER TABLE {table_name} REPLICA IDENTITY FULL",
            f"INSERT INTO {table_name} VALUES (1)",
            f"CREATE SEQUENCE {sequence_name}",
            "COMMIT",
        )
        subscriber.sql_batch(
            "BEGIN",
            f"CREATE TABLE {table_name}(a int primary key)",
            f"INSERT INTO {table_name} VALUES (1)",
            f"CREATE SEQUENCE {sequence_name} INCREMENT BY 10",
            "COMMIT",
        )

        pub_name = table_name + "_pub"
        pub_seq_name = sequence_name + "_pub"
        sub_name = table_name + "_sub"
        publisher.sql_batch(
            f"CREATE PUBLICATION {pub_name} FOR TABLE {table_name}",
            f"CREATE PUBLICATION {pub_seq_name} FOR ALL SEQUENCES",
        )
        # The tablesync loops forever on the unique-constraint violation, and
        # the sequencesync fails on the increment mismatch.
        subscriber.sql(
            f"CREATE SUBSCRIPTION {sub_name} CONNECTION '{publisher.connstr()}' "
            f"PUBLICATION {pub_name}, {pub_seq_name}"
        )
        publisher.wait_for_catchup(sub_name)

        # Wait for the tablesync and sequencesync errors to be reported.
        subscriber.poll_query_until(
            "SELECT count(1) = 1 FROM pg_stat_subscription_stats "
            f"WHERE subname = '{sub_name}' AND sync_seq_error_count > 0 "
            "AND sync_table_error_count > 0"
        )

        # Restore the default increment so sequencesync can complete.
        subscriber.sql(f"ALTER SEQUENCE {sequence_name} INCREMENT 1")
        subscriber.poll_query_until(
            "SELECT count(1) = 1 FROM pg_subscription_rel "
            f"WHERE srrelid = '{sequence_name}'::regclass AND srsubstate = 'r'"
        )

        # Truncate so the tablesync worker can continue.
        subscriber.sql(f"TRUNCATE {table_name}")
        subscriber.poll_query_until(
            "SELECT count(1) = 1 FROM pg_subscription_rel "
            f"WHERE srrelid = '{table_name}'::regclass AND srsubstate in ('r', 's')"
        )

        assert subscriber.sql(f"SELECT a FROM {table_name}") == 1, (
            f"Check that table '{table_name}' now has 1 row."
        )

        # Insert on the publisher, raising an apply error + insert_exists conflict.
        publisher.sql(f"INSERT INTO {table_name} VALUES (1)")
        subscriber.poll_query_until(
            "SELECT apply_error_count > 0 AND confl_insert_exists > 0 "
            f"FROM pg_stat_subscription_stats WHERE subname = '{sub_name}'"
        )

        # Truncate so the apply worker can continue.
        subscriber.sql(f"TRUNCATE {table_name}")
        # This delete is skipped on the (now empty) subscriber: delete_missing.
        publisher.sql(f"DELETE FROM {table_name}")
        subscriber.poll_query_until(
            "SELECT confl_delete_missing > 0 "
            f"FROM pg_stat_subscription_stats WHERE subname = '{sub_name}'"
        )

        return sub_name

    # No subscription errors before starting logical replication.
    assert subscriber.sql("SELECT count(1) FROM pg_stat_subscription_stats") == 0, (
        "Check that there are no subscription errors before starting logical replication."
    )

    sub1_name = create_sub_pub_w_errors("test_tab1", "test_seq1")

    # All error/conflict counters > 0 and stats_reset is NULL.
    assert subscriber.sql(
        f"""
        SELECT apply_error_count > 0,
               sync_seq_error_count > 0,
               sync_table_error_count > 0,
               confl_insert_exists > 0,
               confl_delete_missing > 0,
               stats_reset IS NULL
        FROM pg_stat_subscription_stats WHERE subname = '{sub1_name}'
        """
    ) == (True, True, True, True, True, True), (
        f"Check that errors and conflicts are > 0 and stats_reset is NULL for '{sub1_name}'."
    )

    # Reset a single subscription's stats.
    subscriber.sql(
        "SELECT pg_stat_reset_subscription_stats((SELECT subid FROM "
        f"pg_stat_subscription_stats WHERE subname = '{sub1_name}'))"
    )
    assert subscriber.sql(
        f"""
        SELECT apply_error_count = 0,
               sync_seq_error_count = 0,
               sync_table_error_count = 0,
               confl_insert_exists = 0,
               confl_delete_missing = 0,
               stats_reset IS NOT NULL
        FROM pg_stat_subscription_stats WHERE subname = '{sub1_name}'
        """
    ) == (True, True, True, True, True, True), (
        f"Confirm errors and conflicts are 0 and stats_reset is not NULL after reset for '{sub1_name}'."
    )

    reset_time1 = subscriber.sql(
        f"SELECT stats_reset::text FROM pg_stat_subscription_stats WHERE subname = '{sub1_name}'"
    )
    # Reset again; the timestamp must advance.
    subscriber.sql(
        "SELECT pg_stat_reset_subscription_stats((SELECT subid FROM "
        f"pg_stat_subscription_stats WHERE subname = '{sub1_name}'))"
    )
    assert (
        subscriber.sql(
            f"SELECT stats_reset > '{reset_time1}'::timestamptz FROM "
            f"pg_stat_subscription_stats WHERE subname = '{sub1_name}'"
        )
        is True
    ), f"Check reset timestamp for '{sub1_name}' is newer after second reset."

    sub2_name = create_sub_pub_w_errors("test_tab2", "test_seq2")

    assert subscriber.sql(
        f"""
        SELECT apply_error_count > 0,
               sync_seq_error_count > 0,
               sync_table_error_count > 0,
               confl_insert_exists > 0,
               confl_delete_missing > 0,
               stats_reset IS NULL
        FROM pg_stat_subscription_stats WHERE subname = '{sub2_name}'
        """
    ) == (True, True, True, True, True, True), (
        f"Confirm errors and conflicts are > 0 and stats_reset is NULL for '{sub2_name}'."
    )

    # Reset all subscriptions.
    subscriber.sql("SELECT pg_stat_reset_subscription_stats(NULL)")
    for sub_name in (sub1_name, sub2_name):
        assert subscriber.sql(
            f"""
            SELECT apply_error_count = 0,
                   sync_seq_error_count = 0,
                   sync_table_error_count = 0,
                   confl_insert_exists = 0,
                   confl_delete_missing = 0,
                   stats_reset IS NOT NULL
            FROM pg_stat_subscription_stats WHERE subname = '{sub_name}'
            """
        ) == (True, True, True, True, True, True), (
            f"Confirm errors and conflicts are 0 and stats_reset is not NULL for '{sub_name}' after reset."
        )

    reset_time1 = subscriber.sql(
        f"SELECT stats_reset::text FROM pg_stat_subscription_stats WHERE subname = '{sub1_name}'"
    )
    reset_time2 = subscriber.sql(
        f"SELECT stats_reset::text FROM pg_stat_subscription_stats WHERE subname = '{sub2_name}'"
    )
    # Reset all again; both timestamps must advance.
    subscriber.sql("SELECT pg_stat_reset_subscription_stats(NULL)")
    assert (
        subscriber.sql(
            f"SELECT stats_reset > '{reset_time1}'::timestamptz FROM "
            f"pg_stat_subscription_stats WHERE subname = '{sub1_name}'"
        )
        is True
    ), f"Confirm that reset timestamp for '{sub1_name}' is newer after second reset."
    assert (
        subscriber.sql(
            f"SELECT stats_reset > '{reset_time2}'::timestamptz FROM "
            f"pg_stat_subscription_stats WHERE subname = '{sub2_name}'"
        )
        is True
    ), f"Confirm that reset timestamp for '{sub2_name}' is newer after second reset."

    # Dropping a subscription removes its stats entry.
    sub1_oid = subscriber.sql(
        f"SELECT oid FROM pg_subscription WHERE subname = '{sub1_name}'"
    )
    subscriber.sql(f"DROP SUBSCRIPTION {sub1_name}")
    assert (
        subscriber.sql(f"SELECT pg_stat_have_stats('subscription', 0, {sub1_oid})")
        is False
    ), f"Subscription stats for subscription '{sub1_name}' should be removed."

    sub2_oid = subscriber.sql(
        f"SELECT oid FROM pg_subscription WHERE subname = '{sub2_name}'"
    )
    # Disassociate sub2 from its slot before dropping.
    subscriber.sql_batch(
        f"ALTER SUBSCRIPTION {sub2_name} DISABLE",
        f"ALTER SUBSCRIPTION {sub2_name} SET (slot_name = NONE)",
    )
    subscriber.sql(f"DROP SUBSCRIPTION {sub2_name}")
    assert (
        subscriber.sql(f"SELECT pg_stat_have_stats('subscription', 0, {sub2_oid})")
        is False
    ), f"Subscription stats for subscription '{sub2_name}' should be removed."

    # DISABLE doesn't wait for the walsender to release the slot; wait for it.
    publisher.poll_query_until(
        f"SELECT EXISTS (SELECT 1 FROM pg_replication_slots "
        f"WHERE slot_name = '{sub2_name}' AND active_pid IS NULL)"
    )
    publisher.sql(f"SELECT pg_drop_replication_slot('{sub2_name}')")
