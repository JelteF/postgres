# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/036_sequences.pl.

Tests that sequences are synced to the subscriber: initial sync via FOR ALL
SEQUENCES, that REFRESH PUBLICATION syncs only newly added sequences while
REFRESH SEQUENCES re-syncs all existing ones, that copy_data=false skips value
sync, and that mismatched/missing/insufficiently-privileged sequences only warn
and let replication continue.
"""

QUOTE = '"regress\'quote"'  # a sequence named regress'quote
SYNCED = "SELECT count(1) = 0 FROM pg_subscription_rel WHERE srsubstate NOT IN ('r')"


def test_sequences(create_pg):
    publisher = create_pg("publisher", allows_streaming="logical")
    subscriber = create_pg("subscriber")
    connstr = publisher.connstr()

    publisher.sql_batch(
        "CREATE TABLE regress_seq_test (v BIGINT)",
        "CREATE SEQUENCE regress_s1",
        f"CREATE SEQUENCE {QUOTE}",
    )
    # The subscriber also has sequences we'll add to the publisher later.
    subscriber.sql_batch(
        "CREATE TABLE regress_seq_test (v BIGINT)",
        "CREATE SEQUENCE regress_s1",
        "CREATE SEQUENCE regress_s2",
        "CREATE SEQUENCE regress_s3",
        f"CREATE SEQUENCE {QUOTE}",
    )
    publisher.sql_batch(
        "INSERT INTO regress_seq_test SELECT nextval('regress_s1') FROM generate_series(1,100)",
        "INSERT INTO regress_seq_test SELECT nextval('\"regress''quote\"') FROM generate_series(1,100)",
    )

    publisher.sql("CREATE PUBLICATION regress_seq_pub FOR ALL SEQUENCES")
    subscriber.sql(
        f"CREATE SUBSCRIPTION regress_seq_sub CONNECTION '{connstr}' "
        "PUBLICATION regress_seq_pub"
    )
    subscriber.poll_query_until(SYNCED)

    assert subscriber.sql("SELECT last_value, is_called FROM regress_s1") == (
        100,
        True,
    ), "initial test data replicated"
    assert subscriber.sql(f"SELECT last_value, is_called FROM {QUOTE}") == (
        100,
        True,
    ), "initial test data replicated for sequence name having quotes"

    # REFRESH PUBLICATION syncs new sequences but not existing ones.
    publisher.sql_batch(
        "CREATE SEQUENCE regress_s2",
        "INSERT INTO regress_seq_test SELECT nextval('regress_s2') FROM generate_series(1,100)",
        "INSERT INTO regress_seq_test SELECT nextval('regress_s1') FROM generate_series(1,100)",
    )
    subscriber.sql("ALTER SUBSCRIPTION regress_seq_sub REFRESH PUBLICATION")
    subscriber.poll_query_until(SYNCED)
    assert publisher.sql("SELECT last_value, is_called FROM regress_s1") == (
        200,
        True,
    ), "Check sequence value in the publisher"
    assert subscriber.sql("SELECT last_value, is_called FROM regress_s1") == (
        100,
        True,
    ), "REFRESH PUBLICATION will not sync existing sequence"
    assert subscriber.sql("SELECT last_value, is_called FROM regress_s2") == (
        100,
        True,
    ), "REFRESH PUBLICATION will sync newly published sequence"

    # REFRESH SEQUENCES re-syncs all existing sequences but not new ones.
    publisher.sql_batch(
        "CREATE SEQUENCE regress_s3",
        "INSERT INTO regress_seq_test SELECT nextval('regress_s3') FROM generate_series(1,100)",
        "INSERT INTO regress_seq_test SELECT nextval('regress_s2') FROM generate_series(1,100)",
    )
    subscriber.sql("ALTER SUBSCRIPTION regress_seq_sub REFRESH SEQUENCES")
    subscriber.poll_query_until(SYNCED)
    assert subscriber.sql("SELECT last_value, is_called FROM regress_s1") == (
        200,
        True,
    ), "REFRESH SEQUENCES will sync existing sequences"
    assert subscriber.sql("SELECT last_value, is_called FROM regress_s2") == (
        200,
        True,
    ), "REFRESH SEQUENCES will sync existing sequences"
    assert subscriber.sql("SELECT last_value, is_called FROM regress_s3") == (
        1,
        False,
    ), "REFRESH SEQUENCES will not sync newly published sequence"

    subscriber.sql(
        "ALTER SUBSCRIPTION regress_seq_sub REFRESH PUBLICATION WITH (copy_data = false)"
    )
    subscriber.poll_query_until(SYNCED)
    assert subscriber.sql("SELECT last_value, is_called FROM regress_s3") == (
        1,
        False,
    ), "REFRESH PUBLICATION will not sync new sequence with copy_data = false"

    # Mismatched and missing sequences only warn.
    publisher.sql("CREATE SEQUENCE regress_s4 START 1 INCREMENT 2")
    subscriber.sql("CREATE SEQUENCE regress_s4 START 10 INCREMENT 2")
    offset = subscriber.current_log_position()
    subscriber.sql("ALTER SUBSCRIPTION regress_seq_sub REFRESH PUBLICATION")
    subscriber.wait_for_log(
        r"WARNING: ( [A-Z0-9]+:)? mismatched or renamed sequence on subscriber "
        r'\("public.regress_s4"\)',
        offset,
    )
    publisher.sql("DROP SEQUENCE regress_s4")
    subscriber.wait_for_log(
        r'WARNING: ( [A-Z0-9]+:)? missing sequence on publisher \("public.regress_s4"\)',
        offset,
    )
    # Recreate so later steps don't keep reporting the missing sequence.
    publisher.sql("CREATE SEQUENCE regress_s4 START 10 INCREMENT 2")

    # Insufficient privileges on a publisher sequence are reported as a
    # permission issue, not as a missing sequence, and replication retries.
    publisher.sql_batch(
        "CREATE ROLE regress_seq_repl LOGIN REPLICATION",
        "GRANT USAGE ON SCHEMA public TO regress_seq_repl",
        "GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO regress_seq_repl",
        "REVOKE ALL ON SEQUENCE regress_s2 FROM regress_seq_repl",
    )
    limited = connstr + " user=regress_seq_repl"
    offset = subscriber.current_log_position()
    subscriber.sql(f"ALTER SUBSCRIPTION regress_seq_sub CONNECTION '{limited}'")
    subscriber.sql("ALTER SUBSCRIPTION regress_seq_sub REFRESH SEQUENCES")
    subscriber.wait_for_log(
        r"WARNING: ( [A-Z0-9]+:)? insufficient privileges on publisher sequence "
        r'\("public.regress_s2"\)',
        offset,
    )

    # A sequence actually removed on the publisher is still reported as missing.
    publisher.sql("DROP SEQUENCE regress_s2")
    offset = subscriber.current_log_position()
    subscriber.sql("ALTER SUBSCRIPTION regress_seq_sub REFRESH SEQUENCES")
    subscriber.wait_for_log(
        r'WARNING: ( [A-Z0-9]+:)? missing sequence on publisher \("public.regress_s2"\)',
        offset,
    )
