# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/scripts/t/011_clusterdb_all.pl.

Exercises clusterdb --all (cluster every database), including that an invalid
database is skipped by --all but cannot be targeted directly, and that
--all --table reclusters a specific table across all databases. The
SQL-in-log checks mirror Perl's issues_sql_like.
"""

from pypg.bins import clusterdb


def test_clusterdb_all(pg):
    # clusterdb --all is incompatible with -d and relies on PGDATABASE, which
    # server=pg sets via connection_env(). With template1 + postgres present,
    # two CLUSTER statements should be logged. (?s) makes . span newlines, like
    # the Perl regex's /s flag, since log_contains uses a plain re.search.
    with pg.log_contains(r"(?s)statement: CLUSTER.*statement: CLUSTER"):
        clusterdb("--all", server=pg)


def test_clusterdb_all_skips_invalid_db(pg):
    # An "invalid" database (datconnlimit = -2) must be skipped by --all rather
    # than aborting the whole run.
    # CREATE DATABASE can't run inside a transaction block, so it can't be part
    # of a multi-statement batch (which is wrapped in an implicit transaction).
    pg.sql("CREATE DATABASE regression_invalid")
    pg.sql(
        "UPDATE pg_database SET datconnlimit = -2 WHERE datname = 'regression_invalid'"
    )
    try:
        clusterdb("--all", server=pg)

        # The same invalid database cannot be targeted directly.
        clusterdb.check_all(
            "--dbname",
            "regression_invalid",
            server=pg,
            exit_code=1,
            stderr=r'FATAL:  cannot connect to invalid database "regression_invalid"',
        )
    finally:
        # datconnlimit = -2 marks the database invalid, so DROP DATABASE refuses
        # it unless FORCE is used; reset the flag first. Shared module server,
        # so this must not leak into sibling tests.
        pg.sql(
            "UPDATE pg_database SET datconnlimit = -1 "
            "WHERE datname = 'regression_invalid'"
        )
        pg.sql("DROP DATABASE regression_invalid")


def test_clusterdb_all_specific_table(pg):
    # A clustered table named test1 in both postgres and template1 should be
    # reclustered by --all --table test1.
    setup = (
        "CREATE TABLE test1 (a int)",
        "CREATE INDEX test1x ON test1 (a)",
        "CLUSTER test1 USING test1x",
    )
    pg.sql_batch(*setup)
    pg.sql_batch_oneshot(*setup, dbname="template1")
    try:
        with pg.log_contains(r"statement: CLUSTER public\.test1"):
            clusterdb("--all", "--table", "test1", server=pg)
    finally:
        pg.sql("DROP TABLE test1")
        pg.sql_oneshot("DROP TABLE test1", dbname="template1")
