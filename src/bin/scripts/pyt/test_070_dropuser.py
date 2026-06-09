# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/scripts/t/070_dropuser.pl."""


def test_standard_options(pg_bin):
    pg_bin.check_help("dropuser")
    pg_bin.check_version("dropuser")
    pg_bin.check_bad_option("dropuser")


def test_dropuser(node, pg_bin, sql_like):
    node.sql("CREATE ROLE regress_foobar1")
    sql_like(node, ["dropuser", "regress_foobar1"], r"statement: DROP ROLE regress_foobar1")

    r = pg_bin.run("dropuser", "regress_nonexistent", server=node)
    assert r.returncode != 0
    assert 'role "regress_nonexistent" does not exist' in r.stderr
