# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/scripts/t/050_dropdb.pl.

Exercises the dropdb client program against a running server: the plain
DROP DATABASE it issues, the WITH (FORCE) variant, failure on a nonexistent
database, and that an invalid database (datconnlimit = -2) can still be
dropped. The SQL-in-log checks mirror Perl's issues_sql_like.
"""

from pypg.bins import dropdb


def test_help_version_options():
    dropdb.check_standard_options()


def test_dropdb_basic(pg):
    pg.sql("CREATE DATABASE foobar1")
    with pg.log_contains(r"statement: DROP DATABASE foobar1"):
        dropdb("foobar1", server=pg)


def test_dropdb_force(pg):
    pg.sql("CREATE DATABASE foobar2")
    with pg.log_contains(r"statement: DROP DATABASE foobar2 WITH \(FORCE\);"):
        dropdb("--force", "foobar2", server=pg)


def test_dropdb_nonexistent(pg):
    dropdb.check_all(
        "nonexistent",
        server=pg,
        exit_code=1,
        stderr=r'database "nonexistent" does not exist',
    )


def test_dropdb_invalid_database(pg):
    # A database flagged invalid (datconnlimit = -2, as a failed CREATE/DROP
    # leaves behind) must still be droppable with dropdb. CREATE DATABASE can't
    # run inside a transaction block, so it can't share sql_batch's single
    # implicit transaction with the UPDATE; send them as separate statements.
    pg.sql("CREATE DATABASE regression_invalid")
    pg.sql(
        "UPDATE pg_database SET datconnlimit = -2 WHERE datname = 'regression_invalid'"
    )
    dropdb("regression_invalid", server=pg)
