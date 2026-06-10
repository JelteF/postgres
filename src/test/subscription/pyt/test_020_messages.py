# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/020_messages.pl.

Tests that logical decoding messages (pg_logical_emit_message) appear in the
pgoutput stream as expected: transactional messages only with the 'messages'
option, non-transactional ones regardless (and even from an aborted
transaction).
"""


def test_messages(create_pg):
    publisher = create_pg(
        "publisher", allows_streaming="logical", conf={"autovacuum": False}
    )
    subscriber = create_pg("subscriber")

    publisher.sql("CREATE TABLE tab_test (a int primary key)")
    subscriber.sql("CREATE TABLE tab_test (a int primary key)")

    publisher.sql("CREATE PUBLICATION tap_pub FOR TABLE tab_test")
    subscriber.sql(
        f"CREATE SUBSCRIPTION tap_sub CONNECTION '{publisher.connstr()}' PUBLICATION tap_pub"
    )
    publisher.wait_for_catchup("tap_sub")

    # Disable the subscription so the slot is left for manual inspection.
    subscriber.sql("ALTER SUBSCRIPTION tap_sub DISABLE")
    publisher.poll_query_until(
        "SELECT COUNT(*) FROM pg_catalog.pg_replication_slots "
        "WHERE slot_name = 'tap_sub' AND active='f'"
    )

    publisher.sql(
        "SELECT pg_logical_emit_message(true, 'pgoutput', 'a transactional message')"
    )
    # 66 77 67 == B M C == BEGIN MESSAGE COMMIT.
    assert publisher.sql(
        "SELECT get_byte(data, 0) FROM pg_logical_slot_peek_binary_changes("
        "'tap_sub', NULL, NULL, 'proto_version', '1', 'publication_names', 'tap_pub', "
        "'messages', 'true')"
    ) == [66, 77, 67], "messages on slot are B M C with message option"

    assert publisher.sql(
        "SELECT get_byte(data, 1), encode(substr(data, 11, 8), 'escape') "
        "FROM pg_logical_slot_peek_binary_changes('tap_sub', NULL, NULL, "
        "'proto_version', '1', 'publication_names', 'tap_pub', 'messages', 'true') "
        "OFFSET 1 LIMIT 1"
    ) == (1, "pgoutput"), "flag transactional is set to 1 and prefix is pgoutput"

    # Without the messages option (and with empty-transaction optimization) the
    # message and its BEGIN/COMMIT are not present.
    assert (
        publisher.sql(
            "SELECT get_byte(data, 0) FROM pg_logical_slot_get_binary_changes("
            "'tap_sub', NULL, NULL, 'proto_version', '1', 'publication_names', 'tap_pub')"
        )
        == []
    ), "option messages defaults to false so message (M) is not on slot"

    publisher.sql("INSERT INTO tab_test VALUES (1)")
    message_lsn = publisher.sql(
        "SELECT pg_logical_emit_message(false, 'pgoutput', 'a non-transactional message')"
    )
    publisher.sql("INSERT INTO tab_test VALUES (2)")
    assert publisher.sql(
        "SELECT get_byte(data, 0), get_byte(data, 1) "
        "FROM pg_logical_slot_get_binary_changes('tap_sub', NULL, NULL, "
        "'proto_version', '1', 'publication_names', 'tap_pub', 'messages', 'true') "
        f"WHERE lsn = '{message_lsn}' AND xid = 0"
    ) == (77, 0), "non-transactional message on slot is M"

    # A non-transactional message emitted in an aborted transaction still shows
    # up; force a WAL switch so it gets decoded.
    publisher.sql_batch(
        "BEGIN",
        """SELECT pg_logical_emit_message(false, 'pgoutput',
            'a non-transactional message is available even if the transaction is aborted 1')""",
        "INSERT INTO tab_test VALUES (3)",
        """SELECT pg_logical_emit_message(true, 'pgoutput',
            'a transactional message is not available if the transaction is aborted')""",
        """SELECT pg_logical_emit_message(false, 'pgoutput',
            'a non-transactional message is available even if the transaction is aborted 2')""",
        "ROLLBACK",
        "SELECT pg_switch_wal()",
    )
    assert publisher.sql(
        "SELECT get_byte(data, 0), get_byte(data, 1) "
        "FROM pg_logical_slot_peek_binary_changes('tap_sub', NULL, NULL, "
        "'proto_version', '1', 'publication_names', 'tap_pub', 'messages', 'true')"
    ) == [(77, 0), (77, 0)], (
        "non-transactional message on slot from aborted transaction is M"
    )
