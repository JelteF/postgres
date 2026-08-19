# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/scripts/t/101_vacuumdb_all.pl.

Exercises vacuumdb --all (vacuum every database), including that an "invalid"
database (datconnlimit = -2) is skipped by --all rather than aborting the run,
but cannot be targeted directly. The SQL-in-log check mirrors Perl's
issues_sql_like.
"""

from pypg.bins import vacuumdb


def test_vacuum_all_databases(pg):
    # Bare --all vacuums every database, so with template1 + postgres present
    # more than one VACUUM is logged. (?s) makes . span newlines, like the Perl
    # regex's /s flag, since log_contains uses a plain re.search.
    with pg.log_contains(r"(?s)statement: VACUUM.*statement: VACUUM"):
        vacuumdb("--all", server=pg)


def test_vacuum_all_skips_invalid_db(pg):
    # An "invalid" database (datconnlimit = -2) must be skipped by --all rather
    # than aborting the whole run, and cannot be targeted directly.
    # CREATE DATABASE cannot run inside a transaction block, so the two
    # statements must be issued as separate top-level commands rather than one
    # ;-joined sql_batch (which PQexec wraps in a single implicit transaction).
    pg.sql("CREATE DATABASE regression_invalid")
    pg.sql(
        "UPDATE pg_database SET datconnlimit = -2 WHERE datname = 'regression_invalid'"
    )
    try:
        vacuumdb("--all", server=pg)

        # The same invalid database cannot be targeted directly. (Doesn't quite
        # belong here, but avoids creating an invalid database again elsewhere.)
        vacuumdb.check_all(
            "--dbname",
            "regression_invalid",
            server=pg,
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
