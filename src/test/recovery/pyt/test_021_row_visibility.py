# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/021_row_visibility.pl.

Checks that snapshots on a hot standby behave reasonably: replayed committed
changes become visible, an open transaction's changes that have been streamed
(but not committed) stay invisible until the commit is replayed, and changes
in prepared transactions are invisible until the 2PC is committed.

The Perl test keeps one persistent psql per node; here the primary and standby
reuse their default node.sql() connection, and the primary's open transaction
is held in a separate background session. Standby reads reuse the cached
connection — in read-committed each statement gets a fresh snapshot, so this
sees the latest replayed committed data just as a new connection would.
"""

SELECT_ALL = "SELECT * FROM test_visibility ORDER BY data"


def test_row_visibility(create_pg):
    primary = create_pg(
        "primary", allows_streaming=True, conf={"max_prepared_transactions": 10}
    )
    primary.sql("CREATE TABLE public.test_visibility (data text not null)")
    backup = primary.backup("my_backup")
    standby = create_pg("standby", from_backup=backup, streaming_primary=primary)

    # 1. Initial data is the same (empty).
    assert standby.sql(SELECT_ALL) == [], "data not visible"

    # 2. An INSERT is replayed and becomes visible.
    primary.sql("INSERT INTO test_visibility VALUES ('first insert')")
    primary.wait_for_catchup(standby)
    assert standby.sql(SELECT_ALL) == "first insert", "insert visible"

    # 3. Uncommitted changes are not visible, even once their WAL is streamed.
    # The open transaction is held in a separate background session so the
    # txid_current() below (on primary's default connection) can flush its WAL
    # concurrently.
    session = primary.connect()
    session.sql("BEGIN")
    assert (
        session.sql("UPDATE test_visibility SET data = 'first update' RETURNING data")
        == "first update"
    )
    primary.sql("SELECT txid_current()")  # ensure the UPDATE's WAL is flushed
    primary.wait_for_catchup(standby)
    assert standby.sql(SELECT_ALL) == "first insert", "uncommitted update invisible"

    # 4. The commit makes the update visible.
    session.sql("COMMIT")
    primary.wait_for_catchup(standby)
    assert standby.sql(SELECT_ALL) == "first update", "committed update visible"

    # 5. Changes in prepared transactions are invisible while still prepared.
    primary.sql("DELETE FROM test_visibility")  # start from a clean slate
    primary.sql_batch(
        "BEGIN",
        "INSERT INTO test_visibility VALUES('inserted in prepared will_commit')",
        "PREPARE TRANSACTION 'will_commit'",
    )
    primary.sql_batch(
        "BEGIN",
        "INSERT INTO test_visibility VALUES('inserted in prepared will_abort')",
        "PREPARE TRANSACTION 'will_abort'",
    )
    primary.wait_for_catchup(standby)
    assert standby.sql(SELECT_ALL) == [], "uncommitted prepared invisible"

    # Finish the prepared transactions and confirm only the committed one shows.
    primary.sql("COMMIT PREPARED 'will_commit'")
    primary.sql("ROLLBACK PREPARED 'will_abort'")
    primary.wait_for_catchup(standby)
    assert standby.sql(SELECT_ALL) == "inserted in prepared will_commit", (
        "finished prepared visible"
    )

    primary.stop()
    standby.stop()
