# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/scripts/t/090_reindexdb.pl.

Exercises the reindexdb client program against a running server: the REINDEX
SQL it issues for every target kind (database/system/table/index/schema), the
--concurrently and --tablespace and --verbose variants, parallel --jobs
processing, error cases (concurrent system reindex, tablespace move of system
relations), and combinations of object selectors. Several tests also cross-
check relfilenode/OID changes through pg_class to confirm the right relations
were rebuilt.

The SQL-in-log checks mirror Perl's issues_sql_like.
"""

import pytest

from pypg.bins import reindexdb

# Perl sets $ENV{PGOPTIONS} = '--client-min-messages=WARNING' globally so the
# REINDEX VERBOSE notices and CREATE INDEX chatter don't pollute the captured
# output; mirror it on every reindexdb invocation.
_PGOPTIONS = {"PGOPTIONS": "--client-min-messages=WARNING"}

_TBSPACE_NAME = "reindex_tbspace"

# Save a set of index relfilenodes (toast and normal) into index_relfilenodes,
# so they can be compared against pg_class after a REINDEX. Mirrors the Perl
# test's $save_relfilenodes.
_FETCH_TOAST_RELFILENODES = """
    SELECT b.oid::regclass, c.oid::regclass::text, c.oid, c.relfilenode
    FROM pg_class a
        JOIN pg_class b ON (a.oid = b.reltoastrelid)
        JOIN pg_index i on (a.oid = i.indrelid)
        JOIN pg_class c on (i.indexrelid = c.oid)
    WHERE b.oid IN ('pg_constraint'::regclass, 'test1'::regclass)"""
_FETCH_INDEX_RELFILENODES = """
    SELECT i.indrelid, a.oid::regclass::text, a.oid, a.relfilenode
    FROM pg_class a
        JOIN pg_index i ON (i.indexrelid = a.oid)
    WHERE a.relname IN ('pg_constraint_oid_index', 'test1x')"""
_SAVE_RELFILENODES = (
    f"INSERT INTO index_relfilenodes {_FETCH_TOAST_RELFILENODES}",
    f"INSERT INTO index_relfilenodes {_FETCH_INDEX_RELFILENODES}",
)

# Compare the saved relfilenodes against the current contents of pg_class. The
# join is on the index name rather than OID, since CONCURRENTLY changes the OID.
_COMPARE_RELFILENODES = r"""SELECT b.parent::regclass,
    regexp_replace(b.indname::text, '(pg_toast.pg_toast_)\d+(_index)', '\1<oid>\2'),
    CASE WHEN a.oid = b.indoid THEN 'OID is unchanged'
        ELSE 'OID has changed' END,
    CASE WHEN a.relfilenode = b.relfilenode THEN 'relfilenode is unchanged'
        ELSE 'relfilenode has changed' END
    FROM index_relfilenodes b
        JOIN pg_class a ON b.indname::text = a.oid::regclass::text
    ORDER BY b.parent::text, b.indname::text"""


@pytest.fixture(scope="module")
def setup(pg_server_module):
    """One-time module setup mirroring the Perl test's preamble: a tablespace, a
    text-columned test1 table (so it has a toast table), the relfilenode
    tracking table, and the parallel-processing schemas. These objects are
    shared read-only fixtures for every test; the relfilenode tests re-save the
    tracking table before each comparison rather than relying on leftover state.

    Returns the toast table and toast index names of test1 for the tablespace
    error-case tests.
    """
    pg = pg_server_module

    # Create a tablespace for testing. Perl puts it under the cluster basedir
    # (the parent of the data directory).
    tbspace_path = pg.datadir.parent / "regress_reindex_tbspace"
    tbspace_path.mkdir()
    pg.sql(f"CREATE TABLESPACE {_TBSPACE_NAME} LOCATION '{tbspace_path.as_posix()}'")

    # Use text as data type to get a toast table.
    pg.sql_batch("CREATE TABLE test1 (a text)", "CREATE INDEX test1x ON test1 (a)")
    toast_table = pg.sql(
        "SELECT reltoastrelid::regclass FROM pg_class WHERE oid = 'test1'::regclass"
    )
    toast_index = pg.sql(
        "SELECT indexrelid::regclass FROM pg_index "
        "WHERE indrelid = '%s'::regclass" % toast_table
    )

    pg.sql(
        "CREATE TABLE index_relfilenodes "
        "(parent regclass, indname text, indoid oid, relfilenode oid)"
    )

    # Parallel-processing schemas, with two indexes per table so --jobs has work.
    pg.sql_batch(
        "CREATE SCHEMA s1",
        "CREATE TABLE s1.t1(id integer)",
        "CREATE INDEX ON s1.t1(id)",
        "CREATE INDEX i1 ON s1.t1(id)",
        "CREATE SCHEMA s2",
        "CREATE TABLE s2.t2(id integer)",
        "CREATE INDEX ON s2.t2(id)",
        "CREATE INDEX i2 ON s2.t2(id)",
        # empty schema
        "CREATE SCHEMA s3",
    )

    return {"toast_table": toast_table, "toast_index": toast_index}


def _save_relfilenodes(pg):
    """Reset and re-save the relfilenode tracking table before a comparison."""
    pg.sql_batch("TRUNCATE index_relfilenodes", *_SAVE_RELFILENODES)


def test_help_version_options():
    reindexdb.check_standard_options()


def test_reindex_database(pg, setup):
    # REINDEX DATABASE rebuilds the index relfilenodes of normal tables (test1)
    # but leaves the catalog (pg_constraint) and OIDs unchanged.
    _save_relfilenodes(pg)
    with pg.log_contains(r"statement: REINDEX DATABASE postgres;"):
        reindexdb("postgres", server=pg, addenv=_PGOPTIONS)
    info = pg.sql(_COMPARE_RELFILENODES)
    assert info == [
        (
            "pg_constraint",
            "pg_constraint_oid_index",
            "OID is unchanged",
            "relfilenode is unchanged",
        ),
        (
            "pg_constraint",
            "pg_toast.pg_toast_<oid>_index",
            "OID is unchanged",
            "relfilenode is unchanged",
        ),
        (
            "test1",
            "pg_toast.pg_toast_<oid>_index",
            "OID is unchanged",
            "relfilenode has changed",
        ),
        ("test1", "test1x", "OID is unchanged", "relfilenode has changed"),
    ]


def test_reindex_system(pg, setup):
    # REINDEX SYSTEM rebuilds the catalog indexes (pg_constraint) but leaves the
    # normal table (test1) untouched.
    _save_relfilenodes(pg)
    with pg.log_contains(r"statement: REINDEX SYSTEM postgres;"):
        reindexdb("--system", "postgres", server=pg, addenv=_PGOPTIONS)
    info = pg.sql(_COMPARE_RELFILENODES)
    assert info == [
        (
            "pg_constraint",
            "pg_constraint_oid_index",
            "OID is unchanged",
            "relfilenode has changed",
        ),
        (
            "pg_constraint",
            "pg_toast.pg_toast_<oid>_index",
            "OID is unchanged",
            "relfilenode has changed",
        ),
        (
            "test1",
            "pg_toast.pg_toast_<oid>_index",
            "OID is unchanged",
            "relfilenode is unchanged",
        ),
        ("test1", "test1x", "OID is unchanged", "relfilenode is unchanged"),
    ]


def test_reindex_table(pg, setup):
    with pg.log_contains(r"statement: REINDEX TABLE public\.test1;"):
        reindexdb("--table", "test1", "postgres", server=pg, addenv=_PGOPTIONS)


def test_reindex_table_tablespace(pg, setup):
    with pg.log_contains(
        rf"statement: REINDEX \(TABLESPACE {_TBSPACE_NAME}\) TABLE public\.test1;"
    ):
        reindexdb(
            "--table",
            "test1",
            "--tablespace",
            _TBSPACE_NAME,
            "postgres",
            server=pg,
            addenv=_PGOPTIONS,
        )


def test_reindex_index(pg, setup):
    with pg.log_contains(r"statement: REINDEX INDEX public\.test1x;"):
        reindexdb("--index", "test1x", "postgres", server=pg, addenv=_PGOPTIONS)


def test_reindex_schema(pg, setup):
    with pg.log_contains(r"statement: REINDEX SCHEMA pg_catalog;"):
        reindexdb("--schema", "pg_catalog", "postgres", server=pg, addenv=_PGOPTIONS)


def test_reindex_verbose(pg, setup):
    with pg.log_contains(r"statement: REINDEX \(VERBOSE\) TABLE public\.test1;"):
        reindexdb(
            "--verbose", "--table", "test1", "postgres", server=pg, addenv=_PGOPTIONS
        )


def test_reindex_verbose_tablespace(pg, setup):
    with pg.log_contains(
        rf"statement: REINDEX \(VERBOSE, TABLESPACE {_TBSPACE_NAME}\) "
        r"TABLE public\.test1;"
    ):
        reindexdb(
            "--verbose",
            "--table",
            "test1",
            "--tablespace",
            _TBSPACE_NAME,
            "postgres",
            server=pg,
            addenv=_PGOPTIONS,
        )


def test_reindex_database_concurrently(pg, setup):
    # CONCURRENTLY rebuilds the indexes with brand-new OIDs for normal tables.
    _save_relfilenodes(pg)
    with pg.log_contains(r"statement: REINDEX DATABASE CONCURRENTLY postgres;"):
        reindexdb("--concurrently", "postgres", server=pg, addenv=_PGOPTIONS)
    info = pg.sql(_COMPARE_RELFILENODES)
    assert info == [
        (
            "pg_constraint",
            "pg_constraint_oid_index",
            "OID is unchanged",
            "relfilenode is unchanged",
        ),
        (
            "pg_constraint",
            "pg_toast.pg_toast_<oid>_index",
            "OID is unchanged",
            "relfilenode is unchanged",
        ),
        (
            "test1",
            "pg_toast.pg_toast_<oid>_index",
            "OID has changed",
            "relfilenode has changed",
        ),
        ("test1", "test1x", "OID has changed", "relfilenode has changed"),
    ]


def test_reindex_table_concurrently(pg, setup):
    with pg.log_contains(r"statement: REINDEX TABLE CONCURRENTLY public\.test1;"):
        reindexdb(
            "--concurrently",
            "--table",
            "test1",
            "postgres",
            server=pg,
            addenv=_PGOPTIONS,
        )


def test_reindex_index_concurrently(pg, setup):
    with pg.log_contains(r"statement: REINDEX INDEX CONCURRENTLY public\.test1x;"):
        reindexdb(
            "--concurrently",
            "--index",
            "test1x",
            "postgres",
            server=pg,
            addenv=_PGOPTIONS,
        )


def test_reindex_schema_concurrently(pg, setup):
    with pg.log_contains(r"statement: REINDEX SCHEMA CONCURRENTLY public;"):
        reindexdb(
            "--concurrently",
            "--schema",
            "public",
            "postgres",
            server=pg,
            addenv=_PGOPTIONS,
        )


def test_reindex_system_concurrently_fails(pg, setup):
    # CONCURRENTLY cannot be combined with system catalog reindexing.
    reindexdb.check_all(
        "--concurrently",
        "--system",
        "postgres",
        server=pg,
        addenv=_PGOPTIONS,
        exit_code=1,
    )


def test_reindex_verbose_concurrently(pg, setup):
    with pg.log_contains(
        r"statement: REINDEX \(VERBOSE\) TABLE CONCURRENTLY public\.test1;"
    ):
        reindexdb(
            "--concurrently",
            "--verbose",
            "--table",
            "test1",
            "postgres",
            server=pg,
            addenv=_PGOPTIONS,
        )


def test_reindex_verbose_concurrently_tablespace(pg, setup):
    with pg.log_contains(
        rf"statement: REINDEX \(VERBOSE, TABLESPACE {_TBSPACE_NAME}\) "
        r"TABLE CONCURRENTLY public\.test1;"
    ):
        reindexdb(
            "--concurrently",
            "--verbose",
            "--table",
            "test1",
            "--tablespace",
            _TBSPACE_NAME,
            "postgres",
            server=pg,
            addenv=_PGOPTIONS,
        )


# REINDEX TABLESPACE on toast indexes and tables fails because system relations
# cannot be moved. These are kept out of the main regression suite because the
# toast names are unpredictable and CONCURRENTLY cannot run in a transaction
# block (so TRY/CATCH filtering is not possible).
def test_reindex_toast_table_tablespace_fails(pg, setup):
    reindexdb.check_all(
        "--table",
        setup["toast_table"],
        "--tablespace",
        _TBSPACE_NAME,
        "postgres",
        server=pg,
        addenv=_PGOPTIONS,
        exit_code=1,
        stderr=r"cannot move system relation",
    )


def test_reindex_toast_table_concurrently_tablespace_fails(pg, setup):
    reindexdb.check_all(
        "--concurrently",
        "--table",
        setup["toast_table"],
        "--tablespace",
        _TBSPACE_NAME,
        "postgres",
        server=pg,
        addenv=_PGOPTIONS,
        exit_code=1,
        stderr=r"cannot move system relation",
    )


def test_reindex_toast_index_tablespace_fails(pg, setup):
    reindexdb.check_all(
        "--index",
        setup["toast_index"],
        "--tablespace",
        _TBSPACE_NAME,
        "postgres",
        server=pg,
        addenv=_PGOPTIONS,
        exit_code=1,
        stderr=r"cannot move system relation",
    )


def test_reindex_toast_index_concurrently_tablespace_fails(pg, setup):
    reindexdb.check_all(
        "--concurrently",
        "--index",
        setup["toast_index"],
        "--tablespace",
        _TBSPACE_NAME,
        "postgres",
        server=pg,
        addenv=_PGOPTIONS,
        exit_code=1,
        stderr=r"cannot move system relation",
    )


def test_connection_strings(pg, setup):
    # A conninfo dbname argument should be accepted for table/database/system.
    reindexdb(
        "--echo", "--table=pg_am", "dbname=template1", server=pg, addenv=_PGOPTIONS
    )
    reindexdb("--echo", "dbname=template1", server=pg, addenv=_PGOPTIONS)
    reindexdb("--echo", "--system", "dbname=template1", server=pg, addenv=_PGOPTIONS)


def test_parallel_system_fails(pg, setup):
    # Parallel reindexdb cannot process system catalogs.
    reindexdb.check_all(
        "--jobs",
        "2",
        "--system",
        "postgres",
        server=pg,
        addenv=_PGOPTIONS,
        exit_code=1,
    )


def test_parallel_indices(pg, setup):
    reindexdb(
        "--jobs",
        "2",
        "--index",
        "s1.i1",
        "--index",
        "s2.i2",
        "--index",
        "s1.t1_id_idx",
        "--index",
        "s2.t2_id_idx",
        "postgres",
        server=pg,
        addenv=_PGOPTIONS,
    )


def test_parallel_schemas(pg, setup):
    # Parallel schema reindex does a per-table REINDEX. The command ordering is
    # not stable, so only the s1.t1 statement is checked (as in the Perl test).
    with pg.log_contains(r"statement: REINDEX TABLE s1.t1;"):
        reindexdb(
            "--jobs",
            "2",
            "--schema",
            "s1",
            "--schema",
            "s2",
            "postgres",
            server=pg,
            addenv=_PGOPTIONS,
        )


def test_parallel_empty_schema(pg, setup):
    reindexdb("--jobs", "2", "--schema", "s3", server=pg, addenv=_PGOPTIONS)


def test_parallel_database_concurrently(pg, setup):
    reindexdb(
        "--jobs",
        "2",
        "--concurrently",
        "--dbname",
        "postgres",
        server=pg,
        addenv=_PGOPTIONS,
    )


# Combinations of object selectors: --system wins over the more specific filter.
def test_system_and_table(pg, setup):
    with pg.log_contains(r"statement: REINDEX SYSTEM postgres;"):
        reindexdb(
            "--system", "--table", "test1", "postgres", server=pg, addenv=_PGOPTIONS
        )


def test_system_and_index(pg, setup):
    with pg.log_contains(r"statement: REINDEX INDEX public.test1x;"):
        reindexdb(
            "--system", "--index", "test1x", "postgres", server=pg, addenv=_PGOPTIONS
        )


def test_system_and_schema(pg, setup):
    with pg.log_contains(r"statement: REINDEX SCHEMA pg_catalog;"):
        reindexdb(
            "--system",
            "--schema",
            "pg_catalog",
            "postgres",
            server=pg,
            addenv=_PGOPTIONS,
        )
