# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of contrib/pg_visibility/t/001_concurrent_transaction.pl.

Checks that a concurrent transaction doesn't cause false negatives in the
pg_check_visible() function, on both a primary and a streaming standby.
"""


def test_concurrent_transaction(create_pg):
    primary = create_pg("pgvis_main", allows_streaming=True)

    backup = primary.backup("my_backup")
    standby = create_pg("pgvis_standby", from_backup=backup, streaming_primary=primary)

    # A background session holding an open transaction, so its snapshot stays
    # alive while the table below is vacuumed.
    primary.sql("CREATE DATABASE other_database")
    bsession = primary.connect(dbname="other_database")
    bsession.sql_batch("BEGIN", "SELECT txid_current()")

    primary.sql("CREATE EXTENSION pg_visibility")
    primary.sql("CREATE TABLE vacuum_test AS SELECT 42 i")
    primary.sql("VACUUM (disable_page_skipping) vacuum_test")

    # No false negatives on the primary.
    assert primary.sql("SELECT * FROM pg_check_visible('vacuum_test')") == []

    # ... nor on the standby once it has replayed the changes.
    primary.wait_for_catchup(standby)
    assert standby.sql("SELECT * FROM pg_check_visible('vacuum_test')") == []

    bsession.sql("COMMIT")
