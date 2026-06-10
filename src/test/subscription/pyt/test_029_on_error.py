# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/029_on_error.pl.

Tests the disable_on_error subscription option and ALTER SUBSCRIPTION ... SKIP:
a uniqueness violation during initial sync (and later during apply) disables
the subscription, and after extracting the failed transaction's finish LSN from
the server log we can SKIP it and resume replication. Exercises plain commit,
PREPARE/COMMIT PREPARED, and streamed (over logical_decoding_work_mem)
transactions.
"""

import re

# The conflict ERROR + CONTEXT line in the subscriber log; the finish LSN of the
# failed transaction is the trailing capture group.
CONFLICT_RE = re.compile(
    r'conflict detected on relation "public.tbl".*\n'
    r".*DETAIL:.* Could not apply remote change.*\n"
    r'.*Key already exists in unique index "tbl_pkey", modified by .*origin.* '
    r"in transaction \d+ at .*: key .*, local row .*\n"
    r'.*CONTEXT:.* for replication target relation "public.tbl" '
    r"in transaction \d+, finished at ([0-9A-Fa-f]+/[0-9A-Fa-f]+)"
)


def test_on_error(create_pg):
    # A low logical_decoding_work_mem forces the streaming case below.
    publisher = create_pg(
        "publisher",
        allows_streaming="logical",
        conf={"logical_decoding_work_mem": "64kB", "max_prepared_transactions": 10},
    )
    subscriber = create_pg(
        "subscriber",
        conf={"max_prepared_transactions": 10, "track_commit_timestamp": True},
    )

    offset = 0  # byte offset into the subscriber log

    def test_skip_lsn(nonconflict_data, expected, msg):
        """Called after the caller inserted conflicting data: waits for the
        subscription to disable, reads the failed transaction's finish LSN from
        the log, SKIPs it, re-enables, and checks replication resumes."""
        nonlocal offset
        subscriber.poll_query_until(
            "SELECT subenabled = FALSE FROM pg_subscription WHERE subname = 'sub'"
        )

        match = CONFLICT_RE.search(subscriber.log_since(offset))
        assert match, "could not get error-LSN"
        lsn = match.group(1)

        subscriber.sql(f"ALTER SUBSCRIPTION sub SKIP (lsn = '{lsn}')")
        subscriber.sql("ALTER SUBSCRIPTION sub ENABLE")
        subscriber.poll_query_until(
            "SELECT subskiplsn = '0/0' FROM pg_subscription WHERE subname = 'sub'"
        )

        # Confirm the skip in the log and advance the offset for the next call.
        offset = subscriber.wait_for_log(
            rf"logical replication completed skipping transaction at LSN {lsn}",
            offset,
        )

        publisher.sql(f"INSERT INTO tbl VALUES {nonconflict_data}")
        publisher.wait_for_catchup("sub")
        assert subscriber.sql("SELECT count(*) FROM tbl") == expected, msg

    # The subscriber's table has a primary key and a pre-existing conflicting row.
    publisher.sql_batch(
        "CREATE TABLE tbl (i INT, t BYTEA)",
        "ALTER TABLE tbl REPLICA IDENTITY FULL",
        "INSERT INTO tbl VALUES (1, NULL)",
    )
    subscriber.sql_batch(
        "CREATE TABLE tbl (i INT PRIMARY KEY, t BYTEA)",
        "INSERT INTO tbl VALUES (1, NULL)",
    )

    # Initial sync hits the uniqueness violation, disabling the subscription.
    publisher.sql("CREATE PUBLICATION pub FOR TABLE tbl")
    subscriber.sql(
        f"CREATE SUBSCRIPTION sub CONNECTION '{publisher.connstr()}' PUBLICATION pub "
        "WITH (disable_on_error = true, streaming = on, two_phase = on)"
    )
    subscriber.poll_query_until(
        "SELECT subenabled = false FROM pg_catalog.pg_subscription WHERE subname = 'sub'"
    )

    # Clear the conflicting row and re-enable; sync should now complete.
    subscriber.sql("TRUNCATE tbl")
    subscriber.sql("ALTER SUBSCRIPTION sub ENABLE")
    subscriber.wait_for_subscription_sync(publisher, "sub")
    assert subscriber.sql("SELECT COUNT(*) FROM tbl") == 1, (
        "subscription sub replicated data"
    )

    # Plain commit conflict, then skip.
    publisher.sql_batch(
        "BEGIN",
        "INSERT INTO tbl VALUES (1, NULL)",
        "COMMIT",
    )
    test_skip_lsn("(2, NULL)", 2, "test skipping transaction")

    # PREPARE / COMMIT PREPARED conflict, then skip. COMMIT PREPARED cannot run
    # inside a transaction block, so it is a separate statement.
    publisher.sql_batch(
        "BEGIN",
        "UPDATE tbl SET i = 2",
        "PREPARE TRANSACTION 'gtx'",
    )
    publisher.sql("COMMIT PREPARED 'gtx'")
    test_skip_lsn("(3, NULL)", 3, "test skipping prepare and commit prepared ")

    # STREAM COMMIT conflict (exceeds logical_decoding_work_mem), then skip.
    publisher.sql_batch(
        "BEGIN",
        "INSERT INTO tbl SELECT i, sha256(i::text::bytea) FROM generate_series(1, 10000) s(i)",
        "COMMIT",
    )
    test_skip_lsn("(4, sha256(4::text::bytea))", 4, "test skipping stream-commit")

    assert subscriber.sql("SELECT COUNT(*) FROM pg_prepared_xacts") == 0, (
        "check all prepared transactions are resolved on the subscriber"
    )
