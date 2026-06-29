# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/scripts/t/010_clusterdb.pl.

Exercises the clusterdb client program against a running server: the bare
CLUSTER it issues, table targeting (including a nonexistent table), and a
connection-string invocation. The SQL-in-log checks mirror Perl's
issues_sql_like.
"""

from pypg.bins import clusterdb


def test_help_version_options():
    clusterdb.check_standard_options()


def test_clusterdb_basic(pg):
    # Bare clusterdb runs a database-wide CLUSTER.
    with pg.log_contains(r"statement: CLUSTER;"):
        clusterdb(server=pg)


def test_clusterdb_nonexistent_table(pg):
    clusterdb.check_all(
        "--table",
        "nonexistent",
        server=pg,
        exit_code=1,
        stderr=r'relation "nonexistent" does not exist',
    )


def test_clusterdb_specific_table(pg):
    # Set up a clustered table so clusterdb --table has something to recluster.
    pg.sql_batch(
        "CREATE TABLE test1 (a int)",
        "CREATE INDEX test1x ON test1 (a)",
        "CLUSTER test1 USING test1x",
    )
    with pg.log_contains(r"statement: CLUSTER public\.test1;"):
        clusterdb("--table", "test1", server=pg)
    # Shared module server: drop what we created so siblings see a clean slate.
    pg.sql("DROP TABLE test1")


def test_clusterdb_connection_string(pg):
    # A conninfo dbname argument should be accepted; --echo/--verbose just make
    # the run noisier and don't change its success.
    clusterdb("--echo", "--verbose", "dbname=template1", server=pg)
