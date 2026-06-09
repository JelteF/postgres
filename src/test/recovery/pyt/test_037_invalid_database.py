# Copyright (c) 2023-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/037_invalid_database.pl.

Tests handling of databases left invalid by an interrupted DROP DATABASE: an
invalid database cannot be connected to, altered, or used as a template, is
ignored by vac_truncate_clog(), and can still be dropped. Also exercises
interrupting a DROP DATABASE once it has reached its irreversible phase (which
marks the database invalid), by blocking it on a lock and cancelling it.
"""

import warnings

import pytest

from libpq import LibpqError


def test_invalid_database(create_pg):
    node = create_pg(
        "node",
        conf={
            "autovacuum": False,
            "max_prepared_transactions": 5,
            "log_min_duration_statement": 0,
            "log_connections": "receipt",
            "log_disconnections": True,
        },
    )
    # Held connection for most of the test, kept alive across the ALTER DATABASE
    # below that deliberately kills the default cached connection.
    conn = node.connect()

    # Mark a database invalid directly; that is more reliable than racing the
    # required interruption (exercised separately below).
    conn.sql("CREATE DATABASE regression_invalid")
    conn.sql(
        "UPDATE pg_database SET datconnlimit = -2 WHERE datname = 'regression_invalid'"
    )

    # Cannot connect to, ALTER, or use an invalid database as a template.
    with pytest.raises(
        LibpqError, match='cannot connect to invalid database "regression_invalid"'
    ):
        node.connect(dbname="regression_invalid")
    # ALTER raises FATAL (not ERROR), so it terminates the backend; run it on a
    # throwaway connection so our held one survives. Depending on timing libpq
    # surfaces either the FATAL message or an unexpected-close.
    with pytest.raises(
        LibpqError,
        match="cannot alter invalid database|server closed the connection unexpectedly",
    ):
        node.sql("ALTER DATABASE regression_invalid CONNECTION LIMIT 10")
    with pytest.raises(
        LibpqError, match='cannot use invalid database "regression_invalid" as template'
    ):
        conn.sql("CREATE DATABASE copy_invalid TEMPLATE regression_invalid")

    # vac_truncate_clog() ignores invalid databases: give the invalid database
    # an ancient datfrozenxid, then a VACUUM FREEZE must not warn about
    # wraparound (it would if the invalid database were considered).
    conn.sql(
        "UPDATE pg_database SET datfrozenxid = '123456' "
        "WHERE datname = 'regression_invalid'"
    )
    conn.sql("DROP TABLE IF EXISTS foo_tbl")
    conn.sql("CREATE TABLE foo_tbl()")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        conn.sql("VACUUM FREEZE")
    assert not any(
        "not been vacuumed in over 2 billion transactions" in str(w.message)
        for w in caught
    ), "invalid databases are ignored by vac_truncate_clog"

    # An invalid database can still be dropped, and only once.
    conn.sql("DROP DATABASE regression_invalid")
    with pytest.raises(
        LibpqError, match='database "regression_invalid" does not exist'
    ):
        conn.sql("DROP DATABASE regression_invalid")

    # Interrupting DROP DATABASE: it scans pg_tablespace once it reaches the
    # irreversible phase, so hold an AccessExclusiveLock on pg_tablespace via a
    # prepared transaction to block it there, then cancel it.
    # The FATAL ALTER DATABASE above killed the default cached connection;
    # forget it so node.sql() reconnects.
    node.close_default_conn()
    pid = node.sql("SELECT pg_backend_pid()")
    node.sql("CREATE DATABASE regression_invalid_interrupt")
    node.sql_batch("BEGIN", "LOCK pg_tablespace", "PREPARE TRANSACTION 'lock_tblspc'")

    # Dispatch the DROP on the same (default) session; it blocks on the lock.
    # Nothing below needs node.sql() before the future is consumed: the poll
    # runs on its own connection and the cancel goes through ``conn``.
    drop = node.background_sql("DROP DATABASE regression_invalid_interrupt")

    # Once the DROP is waiting for the lock, cancel it.
    node.poll_query_until(
        "SELECT count(*) > 0 FROM pg_locks WHERE NOT granted "
        "AND relation = 'pg_tablespace'::regclass AND mode = 'AccessShareLock'"
    )
    conn.sql(f"SELECT pg_cancel_backend({pid})")
    with pytest.raises(LibpqError, match="canceling statement due to user request"):
        drop.result()

    # The interrupted DROP left the database invalid, so it rejects connections.
    with pytest.raises(
        LibpqError,
        match='cannot connect to invalid database "regression_invalid_interrupt"',
    ):
        node.connect(dbname="regression_invalid_interrupt")

    # Release the lock and finish dropping it.
    node.sql("ROLLBACK PREPARED 'lock_tblspc'")
    node.sql("DROP DATABASE regression_invalid_interrupt")
