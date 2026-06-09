# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/scripts/t/050_dropdb.pl."""


def test_standard_options(pg_bin):
    pg_bin.check_help("dropdb")
    pg_bin.check_version("dropdb")
    pg_bin.check_bad_option("dropdb")


def test_dropdb(node, pg_bin, sql_like):
    node.sql("CREATE DATABASE foobar1")
    sql_like(node, ["dropdb", "foobar1"], r"statement: DROP DATABASE foobar1")

    node.sql("CREATE DATABASE foobar2")
    sql_like(
        node,
        ["dropdb", "--force", "foobar2"],
        r"statement: DROP DATABASE foobar2 WITH \(FORCE\);",
    )

    r = pg_bin.run("dropdb", "nonexistent", server=node)
    assert r.returncode != 0
    assert 'database "nonexistent" does not exist' in r.stderr

    # check that invalid database can be dropped with dropdb
    node.sql("CREATE DATABASE regression_invalid")
    node.sql(
        "UPDATE pg_database SET datconnlimit = -2"
        " WHERE datname = 'regression_invalid'"
    )
    assert pg_bin.run("dropdb", "regression_invalid", server=node).returncode == 0
