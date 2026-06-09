# Copyright (c) 2026, PostgreSQL Global Development Group

"""Port of src/test/modules/test_misc/t/010_index_concurrently_upsert.pl.

Tests INSERT ... ON CONFLICT DO UPDATE running concurrently with CREATE INDEX
CONCURRENTLY and REINDEX CONCURRENTLY. These exercise the fix for "duplicate
key value violates unique constraint" errors that occurred when
infer_arbiter_indexes() only considered indisvalid indexes, so different
transactions could pick different arbiter indexes.

Each scenario drives three sessions through injection points in a specific
interleaving. ``injection_points_set_local()`` makes each attached point fire
only in the session that attached it, so every session parks at exactly the
point it is responsible for. The assertion is that none of the concurrent
statements raise an error: a ``LibpqError`` from any future is a failure.
"""

import pytest

import pypg

pytestmark = pypg.require_injection_points()

# Injection points used by the scenarios below.
CHECK = "check-exclusion-or-unique-constraint-no-conflict"
SPEC = "exec-insert-before-insert-speculative"
SET_DEAD = "reindex-relation-concurrently-before-set-dead"
SWAP = "reindex-relation-concurrently-before-swap"
DEFINE = "define-index-before-set-valid"
INVALIDATE = "invalidate-catalog-snapshot-end"
ANCESTORS = "exec-init-partition-after-get-partition-ancestors"


@pytest.fixture(scope="module")
def node(create_pg_module):
    n = create_pg_module("node")
    n.sql("CREATE EXTENSION injection_points")
    n.sql_batch(
        "CREATE SCHEMA test",
        "CREATE UNLOGGED TABLE test.tblpk (i int PRIMARY KEY, updated_at timestamp)",
        "ALTER TABLE test.tblpk SET (parallel_workers=0)",
        "CREATE TABLE test.tblparted(i int primary key, updated_at timestamp) "
        "PARTITION BY RANGE (i)",
        "CREATE TABLE test.tbl_partition PARTITION OF test.tblparted "
        "FOR VALUES FROM (0) TO (10000) WITH (parallel_workers = 0)",
        "CREATE UNLOGGED TABLE test.tblexpr(i int, updated_at timestamp)",
        "CREATE UNIQUE INDEX tbl_pkey_special ON test.tblexpr(abs(i)) WHERE i < 1000",
        "ALTER TABLE test.tblexpr SET (parallel_workers=0)",
    )
    return n


def attach(session, point):
    """Attach an injection point local to ``session`` so it only fires there."""
    session.sql_batch(
        "SELECT injection_points_set_local()",
        f"SELECT injection_points_attach('{point}', 'wait')",
    )


def wakeup(node, point):
    """Detach and wake an injection point (from a separate session)."""
    node.sql_batch(
        f"SELECT injection_points_detach('{point}')",
        f"SELECT injection_points_wakeup('{point}')",
    )


REINDEX_CASES = [
    ("set_dead", "tblpk", "tblpk_pkey", "(i)"),
    ("swap", "tblpk", "tblpk_pkey", "(i)"),
    ("before", "tblpk", "tblpk_pkey", "(i)"),
    ("set_dead", "tblpk", "tblpk_pkey", "ON CONSTRAINT tblpk_pkey"),
    ("swap", "tblpk", "tblpk_pkey", "ON CONSTRAINT tblpk_pkey"),
    ("before", "tblpk", "tblpk_pkey", "ON CONSTRAINT tblpk_pkey"),
    ("set_dead", "tblparted", "tbl_partition_pkey", "(i)"),
    ("swap", "tblparted", "tbl_partition_pkey", "(i)"),
    ("before", "tblparted", "tbl_partition_pkey", "(i)"),
]


@pytest.mark.parametrize(
    "perm,table,index,conflict",
    REINDEX_CASES,
    ids=[f"{p}-{t}-{c}" for p, t, _, c in REINDEX_CASES],
)
def test_reindex_upsert(node, perm, table, index, conflict):
    """REINDEX CONCURRENTLY interleaved with two upserts, across the set-dead
    and swap reindex phases and three wakeup orderings."""
    point = SWAP if perm == "swap" else SET_DEAD
    upsert = (
        f"INSERT INTO test.{table} VALUES (13, now()) "
        f"ON CONFLICT {conflict} DO UPDATE SET updated_at = now()"
    )
    s1, s2, s3 = node.connect(), node.connect(), node.connect()

    attach(s1, CHECK)
    attach(s2, SPEC)
    attach(s3, point)

    # s3 starts REINDEX and blocks at the chosen reindex phase.
    f3 = s3.background_sql(f"REINDEX INDEX CONCURRENTLY test.{index}")
    node.wait_for_injection_point(point)

    # s1 starts an upsert and blocks at the arbiter-check point.
    f1 = s1.background_sql(upsert)
    node.wait_for_injection_point(CHECK)

    if perm == "before":
        # s2 starts BEFORE the reindex is woken, then s1 wakes first.
        f2 = s2.background_sql(upsert)
        node.wait_for_injection_point(SPEC)
        wakeup(node, CHECK)
        wakeup(node, point)
        wakeup(node, SPEC)
    else:
        wakeup(node, point)
        f2 = s2.background_sql(upsert)
        node.wait_for_injection_point(SPEC)
        if perm == "swap":
            wakeup(node, SPEC)
            wakeup(node, CHECK)
        else:  # set_dead
            wakeup(node, CHECK)
            wakeup(node, SPEC)

    # None of the concurrent statements may error.
    f1.result()
    f2.result()
    f3.result()

    s1.close()
    s2.close()
    s3.close()
    node.sql(f"TRUNCATE TABLE test.{table}")


def test_reindex_partitioned_cache_inval(node):
    """REINDEX on a partitioned table with a cache invalidation occurring
    between two get_partition_ancestors() calls during the upsert."""
    s1, s2 = node.connect(), node.connect()

    attach(s1, ANCESTORS)
    attach(s2, SWAP)

    f2 = s2.background_sql("REINDEX INDEX CONCURRENTLY test.tbl_partition_pkey")
    node.wait_for_injection_point(SWAP)

    f1 = s1.background_sql(
        "INSERT INTO test.tblparted VALUES (13, now()) "
        "ON CONFLICT (i) DO UPDATE SET updated_at = now()"
    )
    node.wait_for_injection_point(ANCESTORS)

    wakeup(node, SWAP)
    wakeup(node, ANCESTORS)

    f1.result()
    f2.result()

    s1.close()
    s2.close()
    node.sql("TRUNCATE TABLE test.tblparted")


def _create_index_upsert(node, index_sql, upsert, truncate_table):
    """CREATE INDEX CONCURRENTLY interleaved with two upserts. s1 attaches both
    the arbiter-check point and the catalog-snapshot point, so its upsert parks
    first at the snapshot invalidation, then at the arbiter check."""
    s1, s2, s3 = node.connect(), node.connect(), node.connect()

    # s1 holds both points. In a cache-clobbering build s1 could hit the
    # snapshot point during attach; in a normal build the attaches simply
    # complete, leaving s1 idle, so no extra handling is needed here.
    attach(s1, CHECK)
    attach(s1, INVALIDATE)
    attach(s2, SPEC)
    attach(s3, DEFINE)

    f3 = s3.background_sql(index_sql)
    node.wait_for_injection_point(DEFINE)

    f1 = s1.background_sql(upsert)
    node.wait_for_injection_point(INVALIDATE)

    # Wake CREATE INDEX, which continues and triggers catalog invalidation.
    wakeup(node, DEFINE)

    f2 = s2.background_sql(upsert)
    node.wait_for_injection_point(SPEC)

    wakeup(node, INVALIDATE)
    node.wait_for_injection_point(CHECK)
    wakeup(node, SPEC)
    wakeup(node, CHECK)

    f1.result()
    f2.result()
    f3.result()

    s1.close()
    s2.close()
    s3.close()
    node.sql(f"TRUNCATE TABLE test.{truncate_table}")


def test_create_index_upsert(node):
    """CREATE INDEX CONCURRENTLY + UPSERT, exercising catalog invalidation."""
    _create_index_upsert(
        node,
        "CREATE UNIQUE INDEX CONCURRENTLY tbl_pkey_duplicate ON test.tblpk(i)",
        "INSERT INTO test.tblpk VALUES (13,now()) "
        "ON CONFLICT (i) DO UPDATE SET updated_at = now()",
        "tblparted",
    )


def test_create_partial_index_upsert(node):
    """CREATE INDEX CONCURRENTLY on a partial index + UPSERT."""
    _create_index_upsert(
        node,
        "CREATE UNIQUE INDEX CONCURRENTLY tbl_pkey_special_duplicate "
        "ON test.tblexpr(abs(i)) WHERE i < 10000",
        "INSERT INTO test.tblexpr VALUES(13,now()) "
        "ON CONFLICT (abs(i)) WHERE i < 100 DO UPDATE SET updated_at = now()",
        "tblexpr",
    )
