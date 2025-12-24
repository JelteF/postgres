# Copyright (c) 2025, PostgreSQL Global Development Group

"""
Tests for query helper functions with type conversion and result simplification.
"""

import pytest


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
    """Query errors raise RuntimeError with actual error message."""

    with pytest.raises(RuntimeError) as exc_info:
        conn.sql("SELECT * FROM nonexistent_table")

    error_msg = str(exc_info.value)
    assert "nonexistent_table" in error_msg or "does not exist" in error_msg


def test_division_by_zero_error(conn):
    """Division by zero raises RuntimeError."""

    with pytest.raises(RuntimeError) as exc_info:
        conn.sql("SELECT 1/0")

    error_msg = str(exc_info.value)
    assert "division by zero" in error_msg.lower()


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
    """Test UUID type conversion."""
    import uuid

    test_uuid = "550e8400-e29b-41d4-a716-446655440000"
    result = conn.sql(f"SELECT '{test_uuid}'::uuid")
    assert result == uuid.UUID(test_uuid)
    assert isinstance(result, uuid.UUID)


def test_uuid_generation(conn):
    """Test generated UUID type conversion."""
    import uuid

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
    import sys
    import logging

    print("This is in a stdout: AAAAAAAAAAAAAA")
    print("This is in stderr: BBBBB", file=sys.stderr)
    logging.error("This is in the python logs: CCCCCCCCC")
    result = conn.sql("""SELECT ARRAY['{"abc": 123, "xyz": 456}'::json]""")
    assert result == [{"abc": 123, "xyz": 789}]


def test_json_array_multiple(conn):
    """Test array of multiple JSON objects."""

    result = conn.sql(
        """SELECT ARRAY['{"a": 1}'::json, '{"b": 2}'::json, '["x", "y"]'::json]"""
    )
    assert result == [{"a": 1}, {"b": 2}, ["x", "z"]]


def test_2d_int_array(conn):
    """Test 2D integer array."""

    result = conn.sql("SELECT ARRAY[[1,2],[3,4]]")
    assert result == [[1, 2], [3, 4]]


def test_2d_text_array(conn):
    """Test 2D integer array."""

    result = conn.sql("SELECT ARRAY[['a','b'],['c','d,e']]")
    assert result == [["a", "b"], ["c", "d,e"]]


@pytest.mark.skip("An arbitrary reason to skip the test.")
def test_3d_int_array(conn):
    """Test 3D integer array."""

    result = conn.sql("SELECT ARRAY[[[1,2],[3,4]],[[5,6],[7,8]]]")
    assert result == [[[1, 2], [3, 4]], [[5, 6], [7, 8]]]


def test_array_with_null(conn):
    """Test array with NULL elements."""

    result = conn.sql("SELECT ARRAY[1, NULL, 3]")
    assert result == [1, None, 3]
