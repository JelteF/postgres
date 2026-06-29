# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/scripts/t/091_reindexdb_all.pl.

Exercises reindexdb --all (reindex every database), including the various
object filters combined with --all, and that an "invalid" database is skipped
by --all but cannot be targeted directly. The SQL-in-log checks mirror Perl's
issues_sql_like.
"""

import pytest

from pypg.bins import reindexdb

# Perl sets $ENV{PGOPTIONS} = '--client-min-messages=WARNING' globally so the
# CREATE INDEX/REINDEX notices don't drown the output; mirror it on every
# reindexdb invocation.
_PGOPTIONS = {"PGOPTIONS": "--client-min-messages=WARNING"}


@pytest.fixture(scope="module", autouse=True)
def setup_test_tables(pg_server_module):
    """Create the test1 table+index in both postgres and template1 once for the
    module. These are read-only targets for the --all REINDEX runs below, so a
    single module-scoped setup is enough and they don't need per-test cleanup
    (the whole server is torn down with the module)."""
    setup = ("CREATE TABLE test1 (a int)", "CREATE INDEX test1x ON test1 (a)")
    pg_server_module.sql_batch(*setup)
    pg_server_module.sql_batch_oneshot(*setup, dbname="template1")


def test_reindex_all_databases(pg):
    # Bare --all reindexes every database, so more than one REINDEX is logged.
    # (?s) so .* spans the log lines between the two statements, like Perl's /s.
    with pg.log_contains(r"(?s)statement: REINDEX.*statement: REINDEX"):
        reindexdb("--all", server=pg, addenv=_PGOPTIONS)


def test_reindex_all_system(pg):
    with pg.log_contains(r"statement: REINDEX SYSTEM postgres"):
        reindexdb("--all", "--system", server=pg, addenv=_PGOPTIONS)


def test_reindex_all_schema(pg):
    with pg.log_contains(r"statement: REINDEX SCHEMA public"):
        reindexdb("--all", "--schema", "public", server=pg, addenv=_PGOPTIONS)


def test_reindex_all_index(pg):
    with pg.log_contains(r"statement: REINDEX INDEX public\.test1x"):
        reindexdb("--all", "--index", "test1x", server=pg, addenv=_PGOPTIONS)


def test_reindex_all_table(pg):
    with pg.log_contains(r"statement: REINDEX TABLE public\.test1"):
        reindexdb("--all", "--table", "test1", server=pg, addenv=_PGOPTIONS)


def test_reindex_all_skips_invalid_db(pg):
    # An "invalid" database (datconnlimit = -2) must be skipped by --all rather
    # than aborting the whole run, and cannot be targeted directly.
    # Separate statements (not a sql_batch): CREATE DATABASE cannot run inside
    # the implicit transaction block a multi-statement simple query creates.
    pg.sql("CREATE DATABASE regression_invalid")
    pg.sql(
        "UPDATE pg_database SET datconnlimit = -2 WHERE datname = 'regression_invalid'"
    )
    try:
        reindexdb("--all", server=pg, addenv=_PGOPTIONS)

        reindexdb.check_all(
            "--dbname",
            "regression_invalid",
            server=pg,
            addenv=_PGOPTIONS,
            exit_code=1,
            stderr=r'FATAL:  cannot connect to invalid database "regression_invalid"',
        )
    finally:
        # datconnlimit = -2 marks the database invalid, so DROP DATABASE refuses
        # it unless the flag is reset first. Shared module server, so this must
        # not leak into sibling tests.
        pg.sql(
            "UPDATE pg_database SET datconnlimit = -1 "
            "WHERE datname = 'regression_invalid'"
        )
        pg.sql("DROP DATABASE regression_invalid")
