# Copyright (c) 2025, PostgreSQL Global Development Group

"""
Tests for libpq error types and SQLSTATE-based exception mapping.
"""

import pytest
from libpq import LibpqError, PostgresNotice


def test_syntax_error(conn):
    """Invalid SQL syntax raises LibpqError with correct SQLSTATE."""
    with pytest.raises(LibpqError) as exc_info:
        conn.sql("SELEC 1")

    err = exc_info.value
    assert err.sqlstate == "42601"
    assert err.sqlstate_class == "42"
    assert "syntax" in str(err).lower()


def test_unique_violation(conn):
    """Unique violation includes all error fields."""
    conn.sql("CREATE TEMP TABLE test_uv (id int CONSTRAINT test_uv_pk PRIMARY KEY)")
    conn.sql("INSERT INTO test_uv VALUES (1)")

    with pytest.raises(LibpqError) as exc_info:
        conn.sql("INSERT INTO test_uv VALUES (1)")

    err = exc_info.value
    assert err.sqlstate == "23505"
    assert err.table_name == "test_uv"
    assert err.constraint_name == "test_uv_pk"
    assert err.detail == "Key (id)=(1) already exists."


def test_notice_includes_error_fields(conn):
    """A NOTICE surfaced as a warning carries the same diagnostic fields as an
    error, not just its message text."""
    with pytest.warns(PostgresNotice, match="heads up") as record:
        conn.sql(
            "DO $$ BEGIN "
            "RAISE NOTICE 'heads up' USING DETAIL = 'more detail', HINT = 'a hint'; "
            "END $$"
        )

    warning = record[0].message
    assert isinstance(warning, PostgresNotice)
    assert warning.severity == "NOTICE"
    assert warning.detail == "more detail"
    assert warning.hint == "a hint"
