# Copyright (c) 2026, PostgreSQL Global Development Group

"""Port of src/test/modules/test_misc/t/013_temp_obj_multisession.pl.

Tests that one session cannot read or modify another session's temporary
table: each session keeps its temp data in its own local buffer pool, so any
command that needs to look at the data must be rejected. DROP TABLE is
intentionally allowed (autovacuum relies on it to clean up orphaned temp
relations), and catalog-only operations on objects over a temp row type work.
"""

import re

import pytest

from libpq import LibpqError

OTHER_TEMP = "cannot access temporary tables of other sessions"


def test_temp_obj_multisession(create_pg):
    node = create_pg("temp_lock")

    # Owner session, kept alive while a second session probes its temp objects.
    # Create the table without an index first, so read paths go straight
    # through the read-stream / buffer-manager entry points.
    psql1 = node.connect()
    psql1.sql("CREATE TEMP TABLE foo AS SELECT 42 AS val")

    # Resolve the owner's temp schema so the probing session can fully qualify.
    schema = node.sql(
        "SELECT n.nspname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE relname = 'foo' AND relpersistence = 't'"
    )
    assert re.fullmatch(r"pg_temp_\d+", schema), f"got temp schema: {schema}"

    # All of the probes below run from a single session that is not the owner
    # (psql1). What matters is only that the probing session differs from the
    # owner, so they can share one connection; a failed query just aborts its
    # own (auto-commit) statement and leaves the session usable.
    probe = node.connect()

    # DML/SELECT must read the table's data and so go through the buffer
    # manager; with no index the planner uses the read-stream path.
    for stmt in [
        f"SELECT val FROM {schema}.foo",
        f"INSERT INTO {schema}.foo VALUES (73)",
        f"UPDATE {schema}.foo SET val = NULL",
        f"DELETE FROM {schema}.foo",
        f"MERGE INTO {schema}.foo USING (VALUES (42)) AS s(val) "
        "ON foo.val = s.val WHEN MATCHED THEN DELETE",
        f"COPY {schema}.foo TO STDOUT",
    ]:
        with pytest.raises(LibpqError, match=OTHER_TEMP):
            probe.sql(stmt)

    # DDL/maintenance commands have their own command-specific checks.
    with pytest.raises(
        LibpqError, match="cannot truncate temporary tables of other sessions"
    ):
        probe.sql(f"TRUNCATE TABLE {schema}.foo")
    with pytest.raises(
        LibpqError, match="cannot alter temporary tables of other sessions"
    ):
        probe.sql(f"ALTER TABLE {schema}.foo ALTER COLUMN val TYPE bigint")

    # VACUUM silently skips other sessions' temp tables (no error).
    probe.sql(f"VACUUM {schema}.foo")

    with pytest.raises(
        LibpqError, match="cannot execute CLUSTER on temporary tables of other sessions"
    ):
        probe.sql(f"CLUSTER {schema}.foo")

    probe.close()

    # Create an index to exercise the index-scan buffer path (nbtree).
    psql1.sql("CREATE INDEX ON foo(val)")
    with pytest.raises(LibpqError, match=OTHER_TEMP):
        node.sql_batch(
            "SET enable_seqscan = off",
            f"SELECT val FROM {schema}.foo WHERE val = 42",
        )
    with pytest.raises(
        LibpqError, match="cannot alter temporary tables of other sessions"
    ):
        node.sql(f"ALTER INDEX {schema}.foo_val_idx SET (fillfactor = 50)")

    # A function over the owner's temp row type can be observed via the
    # catalog; ALTER/DROP of it are catalog operations that don't read the
    # table, so they succeed from another session.
    psql1.sql(
        "CREATE FUNCTION pg_temp.foo_id(r foo) RETURNS int LANGUAGE SQL AS 'SELECT r.val'"
    )
    node.sql(
        f"ALTER FUNCTION {schema}.foo_id({schema}.foo) SET search_path = pg_catalog"
    )
    node.sql(f"DROP FUNCTION {schema}.foo_id({schema}.foo)")

    # DROP TABLE on another session's temp table is intentionally allowed.
    node.sql(f"DROP TABLE {schema}.foo")

    # A function whose argument is another session's temp row type is allowed
    # but becomes effectively temporary, with a NOTICE. Capture the NOTICE via
    # the server log (raise log_min_messages on this session so it is logged).
    psql1.sql("CREATE TEMP TABLE foo2 AS SELECT 42 AS val")
    with node.connect() as c:
        c.sql("SET log_min_messages = 'notice'")
        with node.log_contains("will be effectively temporary"):
            c.sql(
                f"CREATE FUNCTION public.cross_session_func(r {schema}.foo2) "
                "RETURNS int LANGUAGE SQL AS 'SELECT 1'"
            )

    # A bare DROP TABLE now fails: cross_session_func depends on foo2's row
    # type. This is a catalog-level error, not a buffer-manager block.
    with pytest.raises(
        LibpqError,
        match=r"cannot drop table .*\.foo2 because other objects depend on it",
    ):
        node.sql(f"DROP TABLE {schema}.foo2")

    foo2_oid = node.sql("SELECT oid FROM pg_class WHERE relname = 'foo2'")

    # A second session takes ACCESS SHARE on foo2. The owner's session-exit
    # cleanup will block trying to take AccessExclusiveLock to drop it.
    psql2 = node.connect()
    psql2.sql_batch("BEGIN", f"LOCK TABLE {schema}.foo2 IN ACCESS SHARE MODE")

    offset = node.current_log_position()
    psql1.close()
    node.wait_for_log(
        rf"waiting for AccessExclusiveLock on relation {foo2_oid}", offset
    )

    # Release the lock; the owner's cleanup can now finish.
    psql2.sql("COMMIT")
    psql2.close()

    # foo2 and the dependent cross_session_func are gone, confirming the
    # owner's cleanup got past the blocked dependency walk and completed.
    node.poll_query_until(
        f"SELECT NOT EXISTS (SELECT 1 FROM pg_class WHERE oid = {foo2_oid})"
    )
    assert (
        node.sql("SELECT count(*) FROM pg_proc WHERE proname = 'cross_session_func'")
        == 0
    )
