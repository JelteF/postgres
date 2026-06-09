# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/scripts/t/091_reindexdb_all.pl."""


def test_reindexdb_all(node, pg_bin, sql_like):
    node.sql("CREATE TABLE test1 (a int); CREATE INDEX test1x ON test1 (a);")
    # Close the template1 connection before regression_invalid is created
    # below, otherwise CREATE DATABASE (which copies template1) rejects it as
    # "being accessed by other users".
    with node.connect(dbname="template1") as t1:
        t1.sql("CREATE TABLE test1 (a int); CREATE INDEX test1x ON test1 (a);")

    sql_like(node, ["reindexdb", "--all"], r"statement: REINDEX.*statement: REINDEX")
    sql_like(
        node,
        ["reindexdb", "--all", "--system"],
        r"statement: REINDEX SYSTEM postgres",
    )
    sql_like(
        node,
        ["reindexdb", "--all", "--schema", "public"],
        r"statement: REINDEX SCHEMA public",
    )
    sql_like(
        node,
        ["reindexdb", "--all", "--index", "test1x"],
        r"statement: REINDEX INDEX public\.test1x",
    )
    sql_like(
        node,
        ["reindexdb", "--all", "--table", "test1"],
        r"statement: REINDEX TABLE public\.test1",
    )

    node.sql("CREATE DATABASE regression_invalid")
    node.sql(
        "UPDATE pg_database SET datconnlimit = -2"
        " WHERE datname = 'regression_invalid'"
    )
    assert pg_bin.run("reindexdb", "--all", server=node).returncode == 0

    r = pg_bin.run("reindexdb", "--dbname", "regression_invalid", server=node)
    assert r.returncode != 0
    assert 'cannot connect to invalid database "regression_invalid"' in r.stderr
