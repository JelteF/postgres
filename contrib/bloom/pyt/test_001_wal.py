# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of contrib/bloom/t/001_wal.pl.

Tests that generic xlog records for the bloom index replicate correctly: a
streaming standby replays bloom-index changes and index-only queries return the
same results on the primary and the standby.
"""

# Force index scans so the bloom index is actually exercised.
SETUP = [
    "SET enable_seqscan=off",
    "SET enable_bitmapscan=on",
    "SET enable_indexscan=on",
]

QUERIES = [
    "SELECT * FROM tst WHERE i = 0",
    "SELECT * FROM tst WHERE i = 3",
    "SELECT * FROM tst WHERE t = 'b'",
    "SELECT * FROM tst WHERE t = 'f'",
    "SELECT * FROM tst WHERE i = 3 AND t = 'c'",
    "SELECT * FROM tst WHERE i = 7 AND t = 'e'",
]


def _query_results(node):
    """Run the test queries on a fresh session (so the enable_* settings apply)
    and return the list of result sets."""
    with node.connect() as conn:
        conn.sql_batch(*SETUP)
        return [conn.sql(q) for q in QUERIES]


def test_wal(create_pg):
    primary = create_pg("primary", allows_streaming=True)
    backup = primary.backup("my_backup")

    standby = create_pg("standby", from_backup=backup, streaming_primary=primary)

    def test_index_replay(name):
        # Wait for the standby to catch up, then compare query results.
        primary.wait_for_catchup(standby)
        assert _query_results(primary) == _query_results(standby), (
            f"{name}: query result matches"
        )

    primary.sql("CREATE EXTENSION bloom")
    primary.sql("CREATE TABLE tst (i int4, t text)")
    primary.sql(
        "INSERT INTO tst SELECT i%10, "
        "substr(encode(sha256(i::text::bytea), 'hex'), 1, 1) "
        "FROM generate_series(1,10000) i"
    )
    primary.sql("CREATE INDEX bloomidx ON tst USING bloom (i, t) WITH (col1 = 3)")

    test_index_replay("initial")

    # Run 10 cycles of table modification, checking replay after each step.
    for i in range(1, 11):
        primary.sql(f"DELETE FROM tst WHERE i = {i}")
        test_index_replay(f"delete {i}")
        primary.sql("VACUUM tst")
        test_index_replay(f"vacuum {i}")
        start, end = 100001 + (i - 1) * 10000, 100000 + i * 10000
        primary.sql(
            "INSERT INTO tst SELECT i%10, "
            "substr(encode(sha256(i::text::bytea), 'hex'), 1, 1) "
            f"FROM generate_series({start},{end}) i"
        )
        test_index_replay(f"insert {i}")
