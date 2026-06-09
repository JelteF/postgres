# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of contrib/amcheck/t/003_cic_2pc.pl.

Tests CREATE INDEX CONCURRENTLY interleaved with prepared-transaction (2PC)
modifications, that prepared transactions block CIC the same way across a
server restart, and a pgbench stress of CIC/REINDEX CONCURRENTLY against
concurrent 2PC INSERTs. Indexes are validated with amcheck after each phase.
"""

from pypg._env import test_timeout_default as _timeout_default
from pypg.bins import pgbench

PGBENCH_SCRIPTS = {
    "003_pgbench_concurrent_2pc": """
BEGIN;
INSERT INTO tbl VALUES(0,'null');
PREPARE TRANSACTION 'c:client_id';
COMMIT PREPARED 'c:client_id';
""",
    "003_pgbench_concurrent_2pc_savepoint": """
BEGIN;
SAVEPOINT s1;
INSERT INTO tbl VALUES(0,'[false, "jnvaba", -76, 7, {"_": [1]}, 9]');
PREPARE TRANSACTION 'c:client_id';
COMMIT PREPARED 'c:client_id';
""",
    "003_pgbench_concurrent_cic": r"""
SELECT pg_try_advisory_lock(42)::integer AS gotlock \gset
\if :gotlock
    DROP INDEX CONCURRENTLY idx;
    CREATE INDEX CONCURRENTLY idx ON tbl(i);
    SELECT bt_index_check('idx',true);
    SELECT pg_advisory_unlock(42);
\endif
""",
    "004_pgbench_concurrent_ric": r"""
SELECT pg_try_advisory_lock(42)::integer AS gotlock \gset
\if :gotlock
    REINDEX INDEX CONCURRENTLY idx;
    SELECT bt_index_check('idx',true);
    SELECT pg_advisory_unlock(42);
\endif
""",
    "005_pgbench_concurrent_cic": r"""
SELECT pg_try_advisory_lock(42)::integer AS gotginlock \gset
\if :gotginlock
    DROP INDEX CONCURRENTLY ginidx;
    CREATE INDEX CONCURRENTLY ginidx ON tbl USING gin(j);
    SELECT gin_index_check('ginidx');
    SELECT pg_advisory_unlock(42);
\endif
""",
    "006_pgbench_concurrent_ric": r"""
SELECT pg_try_advisory_lock(42)::integer AS gotginlock \gset
\if :gotginlock
    REINDEX INDEX CONCURRENTLY ginidx;
    SELECT gin_index_check('ginidx');
    SELECT pg_advisory_unlock(42);
\endif
""",
}


def test_cic_2pc(create_pg, tmp_path):
    node = create_pg(
        "CIC_2PC_test",
        conf={
            "max_prepared_transactions": 10,
            "lock_timeout": 1000 * _timeout_default(),
        },
    )
    node.sql("CREATE EXTENSION amcheck")
    node.sql("CREATE TABLE tbl(i int, j jsonb)")

    # Three overlapping 2PC transactions interleaved with CIC. main_h drives the
    # INSERT/PREPARE sequence; cic_h runs the two CICs (which block until the
    # prepared transactions commit). The CICs are dispatched as separate queued
    # queries because CREATE INDEX CONCURRENTLY can't run in a transaction block
    # (a multi-statement PQexec would be one).
    main_h = node.connect()
    main_h.sql_batch("BEGIN", "INSERT INTO tbl VALUES(0, '[[14,2,3]]')")

    # The btree CIC blocks until the prepared transactions commit; run it in the
    # background so it overlaps the 2PC sequence below. The gin CIC is built
    # afterwards (sequentially on the same connection) -- two CICs on one table
    # run concurrently would deadlock in their wait-for-snapshot phase.
    cic_h = node.connect()
    cic_idx = cic_h.background_sql("CREATE INDEX CONCURRENTLY idx ON tbl(i)")

    main_h.sql("PREPARE TRANSACTION 'a'")
    main_h.sql_batch("BEGIN", "INSERT INTO tbl VALUES(0, '[[14,2,3]]')")
    node.sql("COMMIT PREPARED 'a'")
    main_h.sql_batch(
        "PREPARE TRANSACTION 'b'",
        "BEGIN",
        "INSERT INTO tbl VALUES(0, '\"mary had a little lamb\"')",
    )
    node.sql("COMMIT PREPARED 'b'")
    # COMMIT PREPARED can't share a multi-statement batch (its implicit
    # transaction block); send these as separate commands.
    main_h.sql("PREPARE TRANSACTION 'c'")
    main_h.sql("COMMIT PREPARED 'c'")

    main_h.close()
    cic_idx.result()
    cic_h.sql("CREATE INDEX CONCURRENTLY ginidx ON tbl USING gin(j)")
    cic_h.close()

    with node.connect() as check_h:
        check_h.sql("SELECT bt_index_check('idx',true)")
        check_h.sql("SELECT gin_index_check('ginidx')")

    # A prepared xact must block CIC the same way after a restart.
    node.sql_batch(
        "BEGIN",
        'INSERT INTO tbl VALUES(0, \'{"a":[["b",{"x":1}],["b",{"x":2}]],"c":3}\')',
        "PREPARE TRANSACTION 'spans_restart'",
        "BEGIN",
        "CREATE TABLE unused ()",
        "PREPARE TRANSACTION 'persists_forever'",
    )
    node.pg_ctl("restart")

    # These DROP/CREATE CONCURRENTLY run in order on one connection. The first
    # blocks until spans_restart commits (it touched tbl); persists_forever only
    # touched another table, so it does not block. Once the first is unblocked
    # the rest no longer block, so they run synchronously after it.
    reindex_h = node.connect()
    drop_idx = reindex_h.background_sql("DROP INDEX CONCURRENTLY idx")
    node.sql("COMMIT PREPARED 'spans_restart'")
    drop_idx.result()
    reindex_h.sql("CREATE INDEX CONCURRENTLY idx ON tbl(i)")
    reindex_h.sql("DROP INDEX CONCURRENTLY ginidx")
    reindex_h.sql("CREATE INDEX CONCURRENTLY ginidx ON tbl USING gin(j)")
    reindex_h.close()

    with node.connect() as check_h:
        check_h.sql("SELECT bt_index_check('idx',true)")
        check_h.sql("SELECT gin_index_check('ginidx')")

    # Stress CIC + REINDEX CONCURRENTLY + 2PC with pgbench. The CIC/RIC scripts
    # self-serialize on an advisory lock to avoid deadlock.
    node.sql("REINDEX TABLE tbl")  # fix the index left broken above

    scripts = []
    for name, body in PGBENCH_SCRIPTS.items():
        path = tmp_path / f"{name}.sql"
        path.write_text(body)
        scripts += ["-f", str(path)]

    r = pgbench.check_all(
        "--no-vacuum",
        "--client=5",
        "--transactions=100",
        *scripts,
        exit_code=0,
        stdout="actually processed",
        server=node,
    )
    assert r.stderr == ""
