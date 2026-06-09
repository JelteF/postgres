# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/021_row_visibility.pl.

Checks that snapshots on a hot standby behave reasonably: replayed committed
changes become visible, an open transaction's changes that have been streamed
(but not committed) stay invisible until the commit is replayed, and changes
in prepared transactions are invisible until the 2PC is committed.

The Perl test keeps one persistent psql per node; here the primary and standby
each hold one connection, and the primary's open transaction is held in a
separate background session. Standby reads reuse the held connection — in
read-committed each statement gets a fresh snapshot, so this sees the latest
replayed committed data just as a new connection would.
"""

SELECT_ALL = "SELECT * FROM test_visibility ORDER BY data"


def test_row_visibility(create_pg):
    primary = create_pg(
        "primary", allows_streaming=True, conf=["max_prepared_transactions=10"]
    )
    pconn = primary.connect()
    pconn.sql("CREATE TABLE public.test_visibility (data text not null)")
    backup = primary.backup("my_backup")
    standby = create_pg("standby", from_backup=backup, streaming_primary=primary)

    # The standby reads run on one held connection; in read-committed each
    # statement still gets a fresh snapshot, so this sees the latest replayed
    # committed data just as a new connection would.
    sconn = standby.connect()

    # 1. Initial data is the same (empty).
    assert sconn.sql(SELECT_ALL) == [], "data not visible"

    # 2. An INSERT is replayed and becomes visible.
    pconn.sql("INSERT INTO test_visibility VALUES ('first insert')")
    primary.wait_for_catchup(standby)
    assert sconn.sql(SELECT_ALL) == "first insert", "insert visible"

    # 3. Uncommitted changes are not visible, even once their WAL is streamed.
    # The open transaction is held in a separate background session so the
    # txid_current() below (on pconn) can flush its WAL concurrently.
    session = primary.background()
    session.sql("BEGIN")
    assert (
        session.sql("UPDATE test_visibility SET data = 'first update' RETURNING data")
        == "first update"
    )
    pconn.sql("SELECT txid_current()")  # ensure the UPDATE's WAL is flushed
    primary.wait_for_catchup(standby)
    assert sconn.sql(SELECT_ALL) == "first insert", "uncommitted update invisible"

    # 4. The commit makes the update visible.
    session.sql("COMMIT")
    primary.wait_for_catchup(standby)
    assert sconn.sql(SELECT_ALL) == "first update", "committed update visible"

    # 5. Changes in prepared transactions are invisible while still prepared.
    pconn.sql("DELETE FROM test_visibility")  # start from a clean slate
    pconn.sql(
        "BEGIN; INSERT INTO test_visibility VALUES('inserted in prepared will_commit');"
        " PREPARE TRANSACTION 'will_commit'"
    )
    pconn.sql(
        "BEGIN; INSERT INTO test_visibility VALUES('inserted in prepared will_abort');"
        " PREPARE TRANSACTION 'will_abort'"
    )
    primary.wait_for_catchup(standby)
    assert sconn.sql(SELECT_ALL) == [], "uncommitted prepared invisible"

    # Finish the prepared transactions and confirm only the committed one shows.
    pconn.sql("COMMIT PREPARED 'will_commit'")
    pconn.sql("ROLLBACK PREPARED 'will_abort'")
    primary.wait_for_catchup(standby)
    assert sconn.sql(SELECT_ALL) == "inserted in prepared will_commit", (
        "finished prepared visible"
    )

    primary.stop()
    standby.stop()
