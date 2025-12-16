# Copyright (c) 2025, PostgreSQL Global Development Group

"""
Tests for libpq error types and SQLSTATE-based exception mapping.
"""

import pytest
import libpq


def test_syntax_error(conn):
    """Invalid SQL syntax raises SyntaxError with correct SQLSTATE."""
    with pytest.raises(libpq.errors.SyntaxError) as exc_info:
        conn.sql("SELEC 1")

    err = exc_info.value
    assert err.sqlstate == "42601"
    assert err.sqlstate_class == "42"
    assert "syntax" in str(err).lower()


def test_unique_violation(conn):
    """Unique violation includes all error fields and can be caught as parent class."""
    conn.sql("CREATE TEMP TABLE test_uv (id int CONSTRAINT test_uv_pk PRIMARY KEY)")
    conn.sql("INSERT INTO test_uv VALUES (1)")

    with pytest.raises(libpq.errors.UniqueViolation) as exc_info:
        conn.sql("INSERT INTO test_uv VALUES (1)")

    err = exc_info.value
    assert err.sqlstate == "23505"
    assert err.table_name == "test_uv"
    assert err.constraint_name == "test_uv_pk"
    assert err.detail == "Key (id)=(1) already exists."
