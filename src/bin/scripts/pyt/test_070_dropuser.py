# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/scripts/t/070_dropuser.pl.

Exercises the dropuser client program: the DROP ROLE it issues for an existing
role, and that it fails with a clear error on a nonexistent role. The
SQL-in-log check mirrors Perl's issues_sql_like.
"""

from pypg.bins import dropuser


def test_help_version_options():
    dropuser.check_standard_options()


def test_drop_role(pg):
    pg.sql("CREATE ROLE regress_foobar1")
    with pg.log_contains(r"statement: DROP ROLE regress_foobar1"):
        dropuser("regress_foobar1", server=pg)


def test_fails_nonexistent_user(pg):
    dropuser.check_all(
        "regress_nonexistent",
        server=pg,
        exit_code=1,
        stderr=r'role "regress_nonexistent" does not exist',
    )
