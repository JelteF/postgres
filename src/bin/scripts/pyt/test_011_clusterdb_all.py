# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/scripts/t/011_clusterdb_all.pl."""


def test_clusterdb_all(node, pg_bin, sql_like):
    # clusterdb -a is not compatible with -d.  This relies on PGDATABASE being
    # set, which the node fixture's connection environment does.
    sql_like(node, ["clusterdb", "--all"], r"statement: CLUSTER.*statement: CLUSTER")

    node.sql("CREATE DATABASE regression_invalid")
    node.sql(
        "UPDATE pg_database SET datconnlimit = -2"
        " WHERE datname = 'regression_invalid'"
    )
    assert pg_bin.run("clusterdb", "--all", server=node).returncode == 0

    # Doesn't quite belong here, but don't want to waste time creating an
    # invalid database in test_010_clusterdb as well.
    r = pg_bin.run("clusterdb", "--dbname", "regression_invalid", server=node)
    assert r.returncode != 0
    assert 'cannot connect to invalid database "regression_invalid"' in r.stderr

    node.sql(
        "CREATE TABLE test1 (a int);"
        " CREATE INDEX test1x ON test1 (a);"
        " CLUSTER test1 USING test1x"
    )
    node.connect(dbname="template1").sql(
        "CREATE TABLE test1 (a int);"
        " CREATE INDEX test1x ON test1 (a);"
        " CLUSTER test1 USING test1x"
    )
    sql_like(
        node,
        ["clusterdb", "--all", "--table", "test1"],
        r"statement: CLUSTER public\.test1",
    )
