# Copyright (c) 2025, PostgreSQL Global Development Group

"""
Tests for query helper functions with type conversion and result simplification.
"""

import uuid

import pytest

from libpq import LibpqError


def test_single_cell_int(conn):
    """Single cell integer query returns just the value."""
    result = conn.sql("SELECT 1")
    assert result == 1
    assert isinstance(result, int)


def test_single_cell_string(conn):
    """Single cell string query returns just the value."""
    result = conn.sql("SELECT 'hello'")
    assert result == "hello"
    assert isinstance(result, str)


def test_single_cell_bool(conn):
    """Single cell boolean query returns just the value."""

    result = conn.sql("SELECT true")
    assert result is True
    assert isinstance(result, bool)

    result = conn.sql("SELECT false")
    assert result is False


def test_single_cell_float(conn):
    """Single cell float query returns just the value."""

    result = conn.sql("SELECT 3.14::float4")
    assert isinstance(result, float)
    assert abs(result - 3.14) < 0.01


def test_single_cell_null(conn):
    """Single cell NULL query returns None."""

    result = conn.sql("SELECT NULL")
    assert result is None


def test_single_row_multiple_columns(conn):
    """Single row with multiple columns returns a tuple."""

    result = conn.sql("SELECT 1, 'hello', true")
    assert result == (1, "hello", True)
    assert isinstance(result, tuple)


def test_query_with_params(conn):
    """Values bound to $1, $2, ... placeholders come back round-tripped and
    typed by the casts on the placeholders."""

    result = conn.sql("SELECT $1::int, $2::text, $3::bool", 42, "hello", True)
    assert result == (42, "hello", True)


def test_param_is_not_interpolated(conn):
    """A bound parameter is data, never SQL: a value full of quotes and a
    semicolon comes straight back instead of being parsed as part of the query."""

    injection = "'; DROP TABLE foo; --"
    assert conn.sql("SELECT $1::text", injection) == injection


def test_null_param(conn):
    """A Python None binds as SQL NULL."""

    assert conn.sql("SELECT $1::int", None) is None


def test_copy_to_stdout_returns_bytes(conn):
    """A COPY ... TO STDOUT comes back as the raw bytes of the copy stream."""

    result = conn.sql("COPY (SELECT generate_series(1, 3)) TO STDOUT")
    assert result == b"1\n2\n3\n"


def test_single_column_multiple_rows(conn):
    """Single column with multiple rows returns a list of values."""

    result = conn.sql("SELECT * FROM generate_series(1, 3)")
    assert result == [1, 2, 3]
    assert isinstance(result, list)


def test_multiple_rows_and_columns(conn):
    """Multiple rows and columns returns list of tuples."""

    result = conn.sql("SELECT * FROM (VALUES (1, 'a'), (2, 'b'), (3, 'c')) AS t")
    assert result == [(1, "a"), (2, "b"), (3, "c")]
    assert isinstance(result, list)
    assert all(isinstance(row, tuple) for row in result)


def test_empty_result(conn):
    """Empty result set returns empty list."""

    result = conn.sql("SELECT 1 WHERE false")
    assert result == []


def test_query_error_handling(conn):
    """Query errors raise LibpqError carrying the server's error message."""

    with pytest.raises(LibpqError, match='relation "nonexistent_table" does not exist'):
        conn.sql("SELECT * FROM nonexistent_table")


def test_division_by_zero_error(conn):
    """Division by zero raises LibpqError."""

    with pytest.raises(LibpqError, match="division by zero"):
        conn.sql("SELECT 1/0")


def test_simple_exec_create_table(conn):
    """sql for CREATE TABLE returns None."""

    result = conn.sql("CREATE TEMP TABLE test_table (id int, name text)")
    assert result is None

    # Verify table was created
    count = conn.sql("SELECT COUNT(*) FROM test_table")
    assert count == 0


def test_simple_exec_insert(conn):
    """sql for INSERT returns None."""

    conn.sql("CREATE TEMP TABLE test_table (id int, name text)")
    result = conn.sql("INSERT INTO test_table VALUES (1, 'Alice'), (2, 'Bob')")
    assert result is None

    # Verify data was inserted
    count = conn.sql("SELECT COUNT(*) FROM test_table")
    assert count == 2


def test_sql_batch(conn):
    """sql_batch runs several statements like consecutive sql() calls and
    returns every statement's result."""

    results = conn.sql_batch(
        "CREATE TEMP TABLE batch (id int, name text)",
        "INSERT INTO batch VALUES (1, 'Alice'), (2, 'Bob')",
        "SELECT * FROM batch ORDER BY id",
    )
    assert results == [None, None, [(1, "Alice"), (2, "Bob")]]


def test_sql_batch_required_for_multiple_statements(conn):
    """A single sql() call rejects multiple statements; sql_batch takes them
    as separate arguments."""

    with pytest.raises(LibpqError, match="cannot insert multiple commands"):
        conn.sql("SELECT 1; SELECT 2")

    assert conn.sql_batch("SELECT 1", "SELECT 2") == [1, 2]


def test_sql_batch_raises_on_failing_statement(conn):
    """A failing statement raises; the statements before it have already
    executed and committed."""

    conn.sql("CREATE TEMP TABLE batch_fail (id int)")
    with pytest.raises(LibpqError, match="division by zero"):
        conn.sql_batch("INSERT INTO batch_fail VALUES (1)", "SELECT 1/0")
    assert conn.sql("SELECT count(*) FROM batch_fail") == 1


def test_close_portal_releases_snapshot(pg, conn):
    """Inside an open transaction the last statement's unnamed portal keeps
    its snapshot registered (backend_xmin stays set); close_portal releases
    it while the transaction stays open."""

    xmin_query = "SELECT backend_xmin FROM pg_stat_activity WHERE pid = $1"

    _, pid = conn.sql_batch("BEGIN", "SELECT pg_backend_pid()")
    assert pg.sql(xmin_query, pid) is not None

    conn.close_portal("")
    assert pg.sql(xmin_query, pid) is None
    assert conn.sql("SELECT 'transaction still open'") == "transaction still open"
    conn.sql("COMMIT")


def test_sql_batch_no_implicit_transaction(conn):
    """Unlike a simple-protocol multi-statement message, the batch is not
    wrapped in an implicit transaction, so statements that refuse to run in
    one work fine."""

    conn.sql_batch("CREATE TEMP TABLE batch_vac (id int)", "VACUUM batch_vac")


def test_type_conversion_mixed(conn):
    """Test mixed type conversion in a single row."""

    result = conn.sql("SELECT 42::int4, 123::int8, 3.14::float8, 'text', true, NULL")
    assert result == (42, 123, 3.14, "text", True, None)
    assert isinstance(result[0], int)
    assert isinstance(result[1], int)
    assert isinstance(result[2], float)
    assert isinstance(result[3], str)
    assert isinstance(result[4], bool)
    assert result[5] is None


def test_multiple_queries_same_connection(conn):
    """Test running multiple queries on the same connection."""

    result1 = conn.sql("SELECT 1")
    assert result1 == 1

    result2 = conn.sql("SELECT 'hello', 'world'")
    assert result2 == ("hello", "world")

    result3 = conn.sql("SELECT * FROM generate_series(1, 5)")
    assert result3 == [1, 2, 3, 4, 5]


def test_date_type(conn):
    """Test date type conversion."""
    import datetime

    result = conn.sql("SELECT '2025-10-20'::date")
    assert result == datetime.date(2025, 10, 20)
    assert isinstance(result, datetime.date)


def test_timestamp_type(conn):
    """Test timestamp type conversion."""
    import datetime

    result = conn.sql("SELECT '2025-10-20 15:30:45'::timestamp")
    assert result == datetime.datetime(2025, 10, 20, 15, 30, 45)
    assert isinstance(result, datetime.datetime)


def test_time_type(conn):
    """Test time type conversion."""
    import datetime

    result = conn.sql("SELECT '15:30:45'::time")
    assert result == datetime.time(15, 30, 45)
    assert isinstance(result, datetime.time)


def test_numeric_type(conn):
    """Test numeric/decimal type conversion."""
    import decimal

    result = conn.sql("SELECT 123.456::numeric")
    assert result == decimal.Decimal("123.456")
    assert isinstance(result, decimal.Decimal)


def test_int_array(conn):
    """Test integer array type conversion."""

    result = conn.sql("SELECT ARRAY[1, 2, 3, 4, 5]")
    assert result == [1, 2, 3, 4, 5]
    assert isinstance(result, list)
    assert all(isinstance(x, int) for x in result)


def test_text_array(conn):
    """Test text array type conversion."""

    result = conn.sql("SELECT ARRAY['hello', 'world', 'test']")
    assert result == ["hello", "world", "test"]
    assert isinstance(result, list)
    assert all(isinstance(x, str) for x in result)


def test_bool_array(conn):
    """Test boolean array type conversion."""

    result = conn.sql("SELECT ARRAY[true, false, true]")
    assert result == [True, False, True]
    assert isinstance(result, list)
    assert all(isinstance(x, bool) for x in result)


def test_empty_array(conn):
    """Test empty array type conversion."""

    result = conn.sql("SELECT ARRAY[]::int[]")
    assert result == []
    assert isinstance(result, list)


def test_json_type(conn):
    """Test JSON type (parsed to dict)."""

    result = conn.sql('SELECT \'{"key": "value"}\'::json')
    assert isinstance(result, dict)
    assert result == {"key": "value"}


def test_jsonb_type(conn):
    """Test JSONB type (parsed to dict)."""

    result = conn.sql('SELECT \'{"name": "test", "count": 42}\'::jsonb')
    assert isinstance(result, dict)
    assert result == {"name": "test", "count": 42}


def test_json_array(conn):
    """Test JSON array type."""

    result = conn.sql("SELECT '[1, 2, 3, 4, 5]'::json")
    assert isinstance(result, list)
    assert result == [1, 2, 3, 4, 5]


def test_json_nested(conn):
    """Test nested JSON object."""

    result = conn.sql(
        'SELECT \'{"user": {"id": 1, "name": "Alice"}, "active": true}\'::json'
    )
    assert isinstance(result, dict)
    assert result == {"user": {"id": 1, "name": "Alice"}, "active": True}


def test_mixed_types_with_arrays(conn):
    """Test mixed types including arrays in a single row."""

    result = conn.sql("SELECT 42, 'text', ARRAY[1, 2, 3], true")
    assert result == (42, "text", [1, 2, 3], True)
    assert isinstance(result[0], int)
    assert isinstance(result[1], str)
    assert isinstance(result[2], list)
    assert isinstance(result[3], bool)


def test_uuid_type(conn):
    """Test UUID type conversion, binding the value as a query parameter."""
    test_uuid = "550e8400-e29b-41d4-a716-446655440000"
    result = conn.sql("SELECT $1::uuid", test_uuid)
    assert result == uuid.UUID(test_uuid)
    assert isinstance(result, uuid.UUID)


def test_uuid_generation(conn):
    """Test generated UUID type conversion."""
    result = conn.sql("SELECT uuidv4()")
    assert isinstance(result, uuid.UUID)
    # Check it's a valid UUID by ensuring it can be converted to string
    assert len(str(result)) == 36  # UUID string format length


def test_text_array_with_commas(conn):
    """Test text array with elements containing commas."""

    result = conn.sql("SELECT ARRAY['A,B', 'C', ' D ']")
    assert result == ["A,B", "C", " D "]


def test_text_array_with_quotes(conn):
    """Test text array with elements containing quotes."""

    result = conn.sql(r"SELECT ARRAY[E'a\"b', 'c']")
    assert result == ['a"b', "c"]


def test_text_array_with_backslash(conn):
    """Test text array with elements containing backslashes."""

    result = conn.sql(r"SELECT ARRAY[E'a\\b', 'c']")
    assert result == ["a\\b", "c"]


def test_json_array_type(conn):
    """Test array of JSON values with embedded quotes and commas."""

    result = conn.sql("""SELECT ARRAY['{"abc": 123, "xyz": 456}'::json]""")
    assert result == [{"abc": 123, "xyz": 456}]


def test_json_array_multiple(conn):
    """Test array of multiple JSON objects."""

    result = conn.sql(
        """SELECT ARRAY['{"a": 1}'::json, '{"b": 2}'::json, '["x", "y"]'::json]"""
    )
    assert result == [{"a": 1}, {"b": 2}, ["x", "y"]]


def test_2d_int_array(conn):
    """Test 2D integer array."""

    result = conn.sql("SELECT ARRAY[[1,2],[3,4]]")
    assert result == [[1, 2], [3, 4]]


def test_2d_text_array(conn):
    """Test 2D integer array."""

    result = conn.sql("SELECT ARRAY[['a','b'],['c','d,e']]")
    assert result == [["a", "b"], ["c", "d,e"]]


def test_3d_int_array(conn):
    """Test 3D integer array."""

    result = conn.sql("SELECT ARRAY[[[1,2],[3,4]],[[5,6],[7,8]]]")
    assert result == [[[1, 2], [3, 4]], [[5, 6], [7, 8]]]


def test_array_with_null(conn):
    """Test array with NULL elements."""

    result = conn.sql("SELECT ARRAY[1, NULL, 3]")
    assert result == [1, None, 3]
