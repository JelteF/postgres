# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of contrib/amcheck/t/003_cic_2pc.pl.

Tests CREATE INDEX CONCURRENTLY interleaved with prepared-transaction (2PC)
modifications, that prepared transactions block CIC the same way across a
server restart, and a pgbench stress of CIC/REINDEX CONCURRENTLY against
concurrent 2PC INSERTs. Indexes are validated with amcheck after each phase.
"""

from pypg._env import test_timeout_default as _timeout_default

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


def test_cic_2pc(create_pg, pg_bin, tmp_path):
    node = create_pg("CIC_2PC_test")
    node.append_conf("max_prepared_transactions = 10")
    node.append_conf(f"lock_timeout = {1000 * _timeout_default()}")
    node.pg_ctl("restart")
    node.sql("CREATE EXTENSION amcheck")
    node.sql("CREATE TABLE tbl(i int, j jsonb)")

    # Three overlapping 2PC transactions interleaved with CIC. main_h drives the
    # INSERT/PREPARE sequence; cic_h runs the two CICs (which block until the
    # prepared transactions commit). The CICs are dispatched as separate queued
    # queries because CREATE INDEX CONCURRENTLY can't run in a transaction block
    # (a multi-statement PQexec would be one).
    main_h = node.background()
    main_h.sql("BEGIN; INSERT INTO tbl VALUES(0, '[[14,2,3]]')")

    cic_h = node.background()
    cic_idx = cic_h.asql("CREATE INDEX CONCURRENTLY idx ON tbl(i)")
    cic_gin = cic_h.asql("CREATE INDEX CONCURRENTLY ginidx ON tbl USING gin(j)")

    main_h.sql("PREPARE TRANSACTION 'a'")
    main_h.sql("BEGIN; INSERT INTO tbl VALUES(0, '[[14,2,3]]')")
    node.sql("COMMIT PREPARED 'a'")
    main_h.sql(
        "PREPARE TRANSACTION 'b';"
        "BEGIN;"
        "INSERT INTO tbl VALUES(0, '\"mary had a little lamb\"')"
    )
    node.sql("COMMIT PREPARED 'b'")
    # COMMIT PREPARED can't share a multi-statement PQexec (its implicit
    # transaction block); psql sends these as separate commands.
    main_h.sql("PREPARE TRANSACTION 'c'")
    main_h.sql("COMMIT PREPARED 'c'")

    main_h.quit()
    cic_idx.result()
    cic_gin.result()
    cic_h.quit()

    node.sql("SELECT bt_index_check('idx',true)")
    node.sql("SELECT gin_index_check('ginidx')")

    # A prepared xact must block CIC the same way after a restart.
    node.sql(
        "BEGIN;"
        "INSERT INTO tbl VALUES(0, '{\"a\":[[\"b\",{\"x\":1}],[\"b\",{\"x\":2}]],\"c\":3}');"
        "PREPARE TRANSACTION 'spans_restart';"
        "BEGIN;"
        "CREATE TABLE unused ();"
        "PREPARE TRANSACTION 'persists_forever'"
    )
    node.pg_ctl("restart")

    reindex_h = node.background()
    drop_idx = reindex_h.asql("DROP INDEX CONCURRENTLY idx")
    make_idx = reindex_h.asql("CREATE INDEX CONCURRENTLY idx ON tbl(i)")
    drop_gin = reindex_h.asql("DROP INDEX CONCURRENTLY ginidx")
    make_gin = reindex_h.asql("CREATE INDEX CONCURRENTLY ginidx ON tbl USING gin(j)")

    # spans_restart touched tbl, so it blocks the (re)index until it commits;
    # persists_forever only touched another table, so it does not block.
    node.sql("COMMIT PREPARED 'spans_restart'")
    for fut in (drop_idx, make_idx, drop_gin, make_gin):
        fut.result()
    reindex_h.quit()

    node.sql("SELECT bt_index_check('idx',true)")
    node.sql("SELECT gin_index_check('ginidx')")

    # Stress CIC + REINDEX CONCURRENTLY + 2PC with pgbench. The CIC/RIC scripts
    # self-serialize on an advisory lock to avoid deadlock.
    node.sql("REINDEX TABLE tbl")  # fix the index left broken above

    scripts = []
    for name, body in PGBENCH_SCRIPTS.items():
        path = tmp_path / f"{name}.sql"
        path.write_text(body)
        scripts += ["-f", str(path)]

    r = pg_bin.run(
        "pgbench", "--no-vacuum", "--client=5", "--transactions=100", *scripts,
        server=node,
    )
    assert r.returncode == 0, "concurrent INSERTs w/ 2PC and CIC"
    assert "actually processed" in r.stdout
    assert r.stderr == ""
