# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of contrib/amcheck/t/002_cic.pl.

Stresses CREATE INDEX CONCURRENTLY with concurrent modifications under pgbench
and verifies the resulting indexes with amcheck, then checks
bt_index_parent_check() on an index built (with CIC) while a row was being
removed by a concurrent transaction.
"""

from pypg._env import test_timeout_default
from pypg.bins import pgbench

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


def test_cic(create_pg, tmp_path):
    # A generous lock_timeout keeps a CIC from blocking forever on a lock.
    node = create_pg("CIC_test", conf={"lock_timeout": 1000 * test_timeout_default()})
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

    # bt_index_parent_check() on an index built with CIC while a row was deleted
    # by a concurrent in-progress transaction (CIC must skip the dead row).
    node.sql("CREATE TABLE quebec(i int primary key)")
    node.sql("INSERT INTO quebec SELECT i FROM generate_series(1, 2) s(i)")

    # This session must hold back the XID horizon (keeping the row deleted
    # below from being vacuumed away) WITHOUT blocking the upcoming CREATE
    # INDEX CONCURRENTLY's wait for old snapshots. The SELECT's unnamed portal
    # would keep its snapshot registered (and backend_xmin set) for the rest of
    # the open transaction, so close it explicitly.
    in_progress = node.connect()
    in_progress.sql_batch("BEGIN", "SELECT pg_current_xact_id()")
    in_progress.close_portal("")

    node.sql("DELETE FROM quebec WHERE i = 1")
    node.sql("CREATE INDEX CONCURRENTLY oscar ON quebec(i)")

    # Succeeds (does not raise) for the CIC index after the removed row.
    node.sql("SELECT bt_index_parent_check('oscar', heapallindexed => true)")

    in_progress.close()
