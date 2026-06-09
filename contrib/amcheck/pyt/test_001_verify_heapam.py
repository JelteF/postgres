# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of contrib/amcheck/t/001_verify_heapam.pl.

Exercises verify_heapam(): an uncorrupted table (and a sequence, which is a heap
under the hood) report nothing across all option combinations, while a table
whose first page has had its line pointers corrupted is reported. Data checksums
are disabled so the hand-corrupted page reads back without a checksum error.
"""

import re
import struct

import pytest

# Line-pointer corruption messages produced by the corrupted first page. The
# packed values are chosen to hit each of these checks in verify_heapam.c.
HEAP_CORRUPTION_RES = [
    r"line pointer redirection to item at offset \d+ precedes minimum offset \d+",
    r"line pointer redirection to item at offset \d+ exceeds maximum offset \d+",
    r"line pointer to page offset \d+ is not maximally aligned",
    r"line pointer length \d+ is less than the minimum tuple header size \d+",
    r"line pointer to page offset \d+ with length \d+ ends beyond maximum page "
    r"offset \d+",
]


@pytest.fixture
def node(create_pg):
    n = create_pg(
        "test", initdb_opts=["--no-data-checksums"], conf={"autovacuum": False}
    )
    n.sql("CREATE EXTENSION amcheck")
    return n


def _relpath(node, relname):
    return node.datadir / node.sql(f"SELECT pg_relation_filepath('{relname}')")


def _verify(conn, function):
    """Run a verify_heapam() variant and return its report rows as a list.

    ``conn`` is anything with a ``.sql`` method -- a server or a held
    connection. Callers that issue many checks in a row pass a single
    connection so they all reuse it."""
    rows = conn.sql(f"SELECT * FROM {function}")
    if rows == [] or rows is None:
        return []
    return rows if isinstance(rows, list) else [rows]


def _detects_no_corruption(conn, function):
    assert _verify(conn, function) == [], function


def _detects_heap_corruption(conn, function):
    text = "\n".join(str(r) for r in _verify(conn, function))
    for pattern in HEAP_CORRUPTION_RES:
        assert re.search(pattern, text), f"{function}: {pattern}"


def _fresh_test_table(node, relname):
    node.sql_batch(
        f"DROP TABLE IF EXISTS {relname} CASCADE",
        f"CREATE TABLE {relname} (a integer, b text)",
        f"ALTER TABLE {relname} SET (autovacuum_enabled=false)",
        f"ALTER TABLE {relname} ALTER b SET STORAGE external",
        f"INSERT INTO {relname} (a, b) "
        f"(SELECT gs, repeat('b',gs*10) FROM generate_series(1,1000) gs)",
    )
    # A couple of locked/updated rows under savepoints, to exercise multixact
    # and update-chain handling.
    node.sql_batch(
        "BEGIN",
        "SAVEPOINT s1",
        f"SELECT 1 FROM {relname} WHERE a = 42 FOR UPDATE",
        f"UPDATE {relname} SET b = b WHERE a = 42",
        "RELEASE s1",
        "SAVEPOINT s1",
        f"SELECT 1 FROM {relname} WHERE a = 42 FOR UPDATE",
        f"UPDATE {relname} SET b = b WHERE a = 42",
        "COMMIT",
    )


def _corrupt_first_page(node, relname):
    relpath = _relpath(node, relname)
    node.stop()
    with open(relpath, "r+b") as f:
        # Corrupt some line pointers (absolute offset 32 = block 0). The values
        # hit the various line-pointer checks on both endiannesses.
        f.seek(32)
        f.write(
            struct.pack(
                "<6L",
                0xAAA15550,
                0xAAA0D550,
                0x00010000,
                0x00008000,
                0x0000800F,
                0x001E8000,
            )
        )
    node.start()


def _check_all_options_uncorrupted(node, relname):
    for stop in ("true", "false"):
        for check_toast in ("true", "false"):
            for skip in ("'none'", "'all-frozen'", "'all-visible'"):
                for startblock in ("NULL", "0"):
                    for endblock in ("NULL", "0"):
                        opts = (
                            f"on_error_stop := {stop}, "
                            f"check_toast := {check_toast}, "
                            f"skip := {skip}, "
                            f"startblock := {startblock}, "
                            f"endblock := {endblock}"
                        )
                        _detects_no_corruption(
                            node, f"verify_heapam('{relname}', {opts})"
                        )


def test_verify_heapam(node):
    # A table with data but no corruption: every option combination is clean.
    _fresh_test_table(node, "test")
    _check_all_options_uncorrupted(node, "test")

    # A corrupt table is reported under several option combinations.
    _fresh_test_table(node, "test")
    _corrupt_first_page(node, "test")
    _detects_heap_corruption(node, "verify_heapam('test')")
    _detects_heap_corruption(node, "verify_heapam('test', skip := 'all-visible')")
    _detects_heap_corruption(node, "verify_heapam('test', skip := 'all-frozen')")
    _detects_heap_corruption(node, "verify_heapam('test', check_toast := false)")
    _detects_heap_corruption(
        node, "verify_heapam('test', startblock := 0, endblock := 0)"
    )

    # A corrupt table with all-frozen data.
    _fresh_test_table(node, "test")
    node.sql("VACUUM (FREEZE, DISABLE_PAGE_SKIPPING) test")
    _detects_no_corruption(node, "verify_heapam('test')")
    _corrupt_first_page(node, "test")
    _detects_heap_corruption(node, "verify_heapam('test')")
    # Skipping all-frozen pages skips the corrupted (frozen) page.
    _detects_no_corruption(node, "verify_heapam('test', skip := 'all-frozen')")

    # A sequence is a heap under the hood; exercise it through its operations,
    # checking it stays corruption-free.
    node.sql_batch(
        "DROP SEQUENCE IF EXISTS test_seq CASCADE",
        "CREATE SEQUENCE test_seq INCREMENT BY 13 MINVALUE 17 START WITH 23",
        "SELECT nextval('test_seq')",
        "SELECT setval('test_seq', currval('test_seq') + nextval('test_seq'))",
    )
    _check_all_options_uncorrupted(node, "test_seq")
    node.sql("SELECT nextval('test_seq')")
    _check_all_options_uncorrupted(node, "test_seq")
    node.sql("SELECT setval('test_seq', 102)")
    _check_all_options_uncorrupted(node, "test_seq")
    node.sql("ALTER SEQUENCE test_seq RESTART WITH 51")
    _check_all_options_uncorrupted(node, "test_seq")
