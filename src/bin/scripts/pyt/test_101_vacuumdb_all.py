# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/scripts/t/101_vacuumdb_all.pl."""


def test_vacuumdb_all(node, pg_bin, sql_like):
    sql_like(node, ["vacuumdb", "--all"], r"statement: VACUUM.*statement: VACUUM")

    node.sql("CREATE DATABASE regression_invalid")
    node.sql(
        "UPDATE pg_database SET datconnlimit = -2"
        " WHERE datname = 'regression_invalid'"
    )
    assert pg_bin.run("vacuumdb", "--all", server=node).returncode == 0

    # Doesn't quite belong here, but don't want to waste time creating an
    # invalid database in test_100_vacuumdb as well.
    r = pg_bin.run("vacuumdb", "--dbname", "regression_invalid", server=node)
    assert r.returncode != 0
    assert 'cannot connect to invalid database "regression_invalid"' in r.stderr
