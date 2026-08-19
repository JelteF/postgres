# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of contrib/amcheck/t/006_verify_gin.pl.

Corrupts GIN index pages on disk in targeted ways (wrong entry order, swapped
column numbers, parent/child key inconsistencies, posting-tree high-key) and
checks that gin_index_check() reports the expected corruption. Data checksums
are disabled so the corrupted blocks can be read back without a checksum error.
"""

import re
import struct

import pytest

from libpq import LibpqError

# Large tuples make the entry-tree split quickly, but stay below the toast
# threshold.
FILLER_SIZE = 1900


def _relpath(node, relname):
    rel = node.sql(f"SELECT pg_relation_filepath('{relname}')")
    return node.datadir / rel


def _replace_block(path, find, replace, blkno, blksize):
    """Substitute bytes within a single block of a relation file. ``find`` is a
    bytes regex; ``replace`` is bytes or a callable taking the match."""
    with open(path, "r+b") as f:
        f.seek(blkno * blksize)
        buf = f.read(blksize)
        buf = re.sub(find, replace, buf, flags=re.DOTALL)
        f.seek(blkno * blksize)
        f.write(buf)


@pytest.fixture(scope="module")
def node(create_pg_module):
    n = create_pg_module(
        "test", initdb_opts=["--no-data-checksums"], conf={"autovacuum": False}
    )
    n.sql("CREATE EXTENSION amcheck")
    n.sql(
        "CREATE OR REPLACE FUNCTION random_string( INT ) RETURNS text AS $$ "
        "SELECT string_agg(substring("
        "'0123456789abcdefghijklmnopqrstuvwxyz', "
        "ceil(random() * 36)::integer, 1), '') from generate_series(1, $1); "
        "$$ LANGUAGE SQL"
    )
    # Cache the block size now; tests query it while the server is stopped.
    n.blksize = int(n.sql("SHOW block_size"))
    return n


def _filler_values(node, prefixes):
    for p in prefixes:
        node.sql(
            f"INSERT INTO test (a) VALUES (('{{' || '{p}' || "
            f"random_string({FILLER_SIZE}) ||'}}')::text[])"
        )


def _check_raises(node, indexname, expected):
    with pytest.raises(LibpqError, match=re.escape(expected)):
        node.sql(f"SELECT gin_index_check('{indexname}')")


WRONG_ORDER = (
    'index "{}" has wrong tuple order on entry tree page, '
    "block 1, offset 2, rightlink 4294967295"
)


def test_invalid_entry_order_leaf_page(node):
    node.sql_batch(
        "DROP TABLE IF EXISTS test",
        "CREATE TABLE test (a text[])",
        "INSERT INTO test (a) VALUES ('{aaaaa,bbbbb}')",
        "CREATE INDEX test_gin_idx ON test USING gin (a)",
    )
    relpath = _relpath(node, "test_gin_idx")
    node.stop()
    # block 1 = root; produce wrong order by replacing aaaaa with ccccc.
    _replace_block(relpath, b"aaaaa", b"ccccc", 1, node.blksize)
    node.start()
    _check_raises(node, "test_gin_idx", WRONG_ORDER.format("test_gin_idx"))


def test_invalid_entry_order_inner_page(node):
    # The inner page needs at least 3 items (rightmost inner key isn't order
    # checked), so insert enough to cause two splits.
    node.sql_batch("DROP TABLE IF EXISTS test", "CREATE TABLE test (a text[])")
    _filler_values(
        node,
        [
            "pppppppppp",
            "qqqqqqqqqq",
            "rrrrrrrrrr",
            "ssssssssss",
            "tttttttttt",
            "uuuuuuuuuu",
            "vvvvvvvvvv",
            "wwwwwwwwww",
        ],
    )
    node.sql("CREATE INDEX test_gin_idx ON test USING gin (a)")
    relpath = _relpath(node, "test_gin_idx")
    node.stop()
    # rrrrrrrrrr and tttttttttt are root keys; break order via rrrrrrrrrr.
    _replace_block(relpath, b"rrrrrrrrrr", b"zzzzzzzzzz", 1, node.blksize)
    node.start()
    _check_raises(node, "test_gin_idx", WRONG_ORDER.format("test_gin_idx"))


def test_invalid_entry_columns_order(node):
    node.sql_batch(
        "DROP TABLE IF EXISTS test",
        "CREATE TABLE test (a text[],b text[])",
        "INSERT INTO test (a,b) VALUES ('{aaa}','{bbb}')",
        "CREATE INDEX test_gin_idx ON test USING gin (a,b)",
    )
    relpath = _relpath(node, "test_gin_idx")
    node.stop()
    # Swap column numbers: root items (1,aaa),(2,bbb) -> (2,aaa),(1,bbb).
    attrno_1 = struct.pack("<h", 1)
    attrno_2 = struct.pack("<h", 2)
    _replace_block(
        relpath,
        re.escape(attrno_1) + b"(.)(aaa)",
        lambda m: attrno_2 + m.group(1) + m.group(2),
        1,
        node.blksize,
    )
    _replace_block(
        relpath,
        re.escape(attrno_2) + b"(.)(bbb)",
        lambda m: attrno_1 + m.group(1) + m.group(2),
        1,
        node.blksize,
    )
    node.start()
    _check_raises(node, "test_gin_idx", WRONG_ORDER.format("test_gin_idx"))


def test_inconsistent_parent_key_parent_corrupted(node):
    node.sql_batch("DROP TABLE IF EXISTS test", "CREATE TABLE test (a text[])")
    _filler_values(
        node, ["llllllllll", "mmmmmmmmmm", "nnnnnnnnnn", "xxxxxxxxxx", "yyyyyyyyyy"]
    )
    node.sql("CREATE INDEX test_gin_idx ON test USING gin (a)")
    relpath = _relpath(node, "test_gin_idx")
    node.stop()
    # nnnnnnnnnn is the root parent key; replace with something smaller than the
    # child's keys.
    _replace_block(relpath, b"nnnnnnnnnn", b"aaaaaaaaaa", 1, node.blksize)
    node.start()
    _check_raises(
        node,
        "test_gin_idx",
        'index "test_gin_idx" has inconsistent records on page 3 offset 3',
    )


def test_inconsistent_parent_key_child_corrupted(node):
    node.sql_batch("DROP TABLE IF EXISTS test", "CREATE TABLE test (a text[])")
    _filler_values(
        node, ["llllllllll", "mmmmmmmmmm", "nnnnnnnnnn", "xxxxxxxxxx", "yyyyyyyyyy"]
    )
    node.sql("CREATE INDEX test_gin_idx ON test USING gin (a)")
    relpath = _relpath(node, "test_gin_idx")
    node.stop()
    # block 3 = leaf; nnnnnnnnnn is the parent key, so make the child key bigger.
    _replace_block(relpath, b"nnnnnnnnnn", b"pppppppppp", 3, node.blksize)
    node.start()
    _check_raises(
        node,
        "test_gin_idx",
        'index "test_gin_idx" has inconsistent records on page 3 offset 3',
    )


def test_inconsistent_parent_key_posting_tree(node):
    node.sql_batch(
        "DROP TABLE IF EXISTS test",
        "CREATE TABLE test (a text[])",
        "INSERT INTO test (a) select ('{aaaaa}') from generate_series(1,10000)",
        "CREATE INDEX test_gin_idx ON test USING gin (a)",
    )
    relpath = _relpath(node, "test_gin_idx")
    node.stop()
    # block 2 = posting tree root (leaves 3 and 4). Replace leaf 4's high key
    # with (1,1) so leaf tids exceed the new high key.
    find = re.escape(struct.pack("<HHH", 0, 4, 0)) + b"...."
    replace = struct.pack("<HHHHH", 0, 4, 0, 1, 1)
    _replace_block(relpath, find, lambda m: replace, 2, node.blksize)
    node.start()
    _check_raises(
        node,
        "test_gin_idx",
        'index "test_gin_idx": tid exceeds parent\'s high key in postingTree '
        "leaf on block 4",
    )
