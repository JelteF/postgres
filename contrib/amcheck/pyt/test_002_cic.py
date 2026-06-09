# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of contrib/amcheck/t/002_cic.pl.

Stresses CREATE INDEX CONCURRENTLY with concurrent modifications under pgbench
and verifies the resulting indexes with amcheck, then checks
bt_index_parent_check() on an index built (with CIC) while a row was being
removed by a concurrent transaction.
"""

from pypg._env import test_timeout_default

# pgbench scripts run with equal weight. The CIC script takes an advisory lock
# so only one CREATE INDEX CONCURRENTLY runs at a time (concurrent CICs on the
# same table would deadlock).
SCRIPT_TXN = """
BEGIN;
INSERT INTO tbl VALUES(0, '{"a":[["b",{"x":1}],["b",{"x":2}]],"c":3}');
COMMIT;
"""

SCRIPT_SAVEPOINTS = """
BEGIN;
SAVEPOINT s1;
INSERT INTO tbl VALUES(0, '[[14,2,3]]');
COMMIT;
"""

SCRIPT_CIC = r"""
SELECT pg_try_advisory_lock(42)::integer AS gotlock \gset
\if :gotlock
    DROP INDEX CONCURRENTLY idx;
    CREATE INDEX CONCURRENTLY idx ON tbl(i);
    DROP INDEX CONCURRENTLY ginidx;
    CREATE INDEX CONCURRENTLY ginidx ON tbl USING gin(j);
    SELECT bt_index_check('idx',true);
    SELECT gin_index_check('ginidx');
    SELECT pg_advisory_unlock(42);
\endif
"""


def test_cic(create_pg, pg_bin, tmp_path):
    node = create_pg("CIC_test")
    # A generous lock_timeout keeps a CIC from blocking forever on a lock.
    node.append_conf(f"lock_timeout = {1000 * test_timeout_default()}")
    node.pg_ctl("restart")
    node.sql("CREATE EXTENSION amcheck")
    node.sql("CREATE TABLE tbl(i int, j jsonb)")
    node.sql("CREATE INDEX idx ON tbl(i)")
    node.sql("CREATE INDEX ginidx ON tbl USING gin(j)")

    scripts = []
    for name, body in [
        ("txn", SCRIPT_TXN),
        ("savepoints", SCRIPT_SAVEPOINTS),
        ("cic", SCRIPT_CIC),
    ]:
        path = tmp_path / f"002_pgbench_{name}.sql"
        path.write_text(body)
        scripts += ["-f", str(path)]

    r = pg_bin.run(
        "pgbench", "--no-vacuum", "--client=5", "--transactions=100", *scripts,
        server=node,
    )
    assert r.returncode == 0, "concurrent INSERTs and CIC"
    assert "actually processed" in r.stdout
    assert r.stderr == ""

    # bt_index_parent_check() on an index built with CIC while a row was deleted
    # by a concurrent in-progress transaction (CIC must skip the dead row).
    node.sql("CREATE TABLE quebec(i int primary key)")
    node.sql("INSERT INTO quebec SELECT i FROM generate_series(1, 2) s(i)")

    in_progress = node.background()
    in_progress.sql("BEGIN; SELECT pg_current_xact_id()")

    node.sql("DELETE FROM quebec WHERE i = 1")
    node.sql("CREATE INDEX CONCURRENTLY oscar ON quebec(i)")

    # Succeeds (does not raise) for the CIC index after the removed row.
    node.sql("SELECT bt_index_parent_check('oscar', heapallindexed => true)")

    in_progress.quit()
