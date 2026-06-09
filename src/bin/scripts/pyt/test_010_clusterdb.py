# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/scripts/t/010_clusterdb.pl."""


def test_standard_options(pg_bin):
    pg_bin.check_help("clusterdb")
    pg_bin.check_version("clusterdb")
    pg_bin.check_bad_option("clusterdb")


def test_clusterdb(node, pg_bin, sql_like):
    sql_like(node, ["clusterdb"], r"statement: CLUSTER;")

    r = pg_bin.run("clusterdb", "--table", "nonexistent", server=node)
    assert r.returncode != 0
    assert 'relation "nonexistent" does not exist' in r.stderr

    node.sql(
        "CREATE TABLE test1 (a int);"
        " CREATE INDEX test1x ON test1 (a);"
        " CLUSTER test1 USING test1x"
    )
    sql_like(node, ["clusterdb", "--table", "test1"], r"statement: CLUSTER public\.test1;")

    r = pg_bin.run("clusterdb", "--echo", "--verbose", "dbname=template1", server=node)
    assert r.returncode == 0, r.stderr
