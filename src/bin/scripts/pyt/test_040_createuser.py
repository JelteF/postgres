# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/scripts/t/040_createuser.pl.

Exercises the createuser client program against a running server: that each
option combination produces the expected CREATE ROLE statement, and that it
fails on a duplicate role or too many non-option arguments. The SQL-in-log
checks mirror Perl's issues_sql_like.

Roles are cluster-wide, so each test drops the roles it creates to avoid
leaking into sibling tests sharing the module server. Tests that reference a
membership/admin role (e.g. --with-admin regress_user1) create that prerequisite
themselves and drop it again, rather than depending on another test's leftovers.
"""

import contextlib

from pypg.bins import createuser


@contextlib.contextmanager
def _drop_roles(pg, *roles):
    """Ensure the given roles are dropped after the block, even on failure.

    Quoting follows what createuser/the tests use; pass the unquoted role name
    and this wraps it for SQL DROP ROLE."""
    try:
        yield
    finally:
        for role in roles:
            pg.sql(f'DROP ROLE IF EXISTS "{role}"')


def test_help_version_options():
    createuser.check_standard_options()


def test_create_user(pg):
    with _drop_roles(pg, "regress_user1"):
        with pg.log_contains(
            r"statement: CREATE ROLE regress_user1 NOSUPERUSER NOCREATEDB"
            r" NOCREATEROLE INHERIT LOGIN NOREPLICATION NOBYPASSRLS;"
        ):
            createuser("regress_user1", server=pg)


def test_create_non_login_role(pg):
    with _drop_roles(pg, "regress_role1"):
        with pg.log_contains(
            r"statement: CREATE ROLE regress_role1 NOSUPERUSER NOCREATEDB"
            r" NOCREATEROLE INHERIT NOLOGIN NOREPLICATION NOBYPASSRLS;"
        ):
            createuser("--no-login", "regress_role1", server=pg)


def test_create_createrole_user(pg):
    with _drop_roles(pg, "regress user2"):
        with pg.log_contains(
            r'statement: CREATE ROLE "regress user2" NOSUPERUSER NOCREATEDB'
            r" CREATEROLE INHERIT LOGIN NOREPLICATION NOBYPASSRLS;"
        ):
            createuser("--createrole", "regress user2", server=pg)


def test_create_superuser(pg):
    with _drop_roles(pg, "regress_user3"):
        with pg.log_contains(
            r"statement: CREATE ROLE regress_user3 SUPERUSER CREATEDB"
            r" CREATEROLE INHERIT LOGIN NOREPLICATION NOBYPASSRLS;"
        ):
            createuser("--superuser", "regress_user3", server=pg)


def test_with_admin_multiple(pg):
    # --with-admin adds the named existing roles as members with admin option of
    # the newly created role, so both must exist first.
    pg.sql_batch(
        "CREATE ROLE regress_user1 LOGIN",
        'CREATE ROLE "regress user2" CREATEROLE LOGIN',
    )
    with _drop_roles(pg, "regress user #4", "regress user2", "regress_user1"):
        with pg.log_contains(
            r'statement: CREATE ROLE "regress user #4" NOSUPERUSER NOCREATEDB'
            r" NOCREATEROLE INHERIT LOGIN NOREPLICATION NOBYPASSRLS"
            r' ADMIN regress_user1,"regress user2";'
        ):
            createuser(
                "--with-admin",
                "regress_user1",
                "--with-admin",
                "regress user2",
                "regress user #4",
                server=pg,
            )


def test_with_member_multiple(pg):
    # --with-member adds the named existing roles as members of the new role.
    pg.sql_batch(
        "CREATE ROLE regress_user3 LOGIN",
        'CREATE ROLE "regress user #4" LOGIN',
    )
    with _drop_roles(pg, "REGRESS_USER5", "regress user #4", "regress_user3"):
        with pg.log_contains(
            r'statement: CREATE ROLE "REGRESS_USER5" NOSUPERUSER NOCREATEDB'
            r" NOCREATEROLE INHERIT LOGIN NOREPLICATION NOBYPASSRLS"
            r' ROLE regress_user3,"regress user #4";'
        ):
            createuser(
                "REGRESS_USER5",
                "--with-member",
                "regress_user3",
                "--with-member",
                "regress user #4",
                server=pg,
            )


def test_valid_until(pg):
    with _drop_roles(pg, "regress_user6"):
        with pg.log_contains(
            r"statement: CREATE ROLE regress_user6 NOSUPERUSER NOCREATEDB"
            r" NOCREATEROLE INHERIT LOGIN NOREPLICATION NOBYPASSRLS"
            r" VALID UNTIL '2029 12 31';"
        ):
            createuser("--valid-until", "2029 12 31", "regress_user6", server=pg)


def test_bypassrls(pg):
    with _drop_roles(pg, "regress_user7"):
        with pg.log_contains(
            r"statement: CREATE ROLE regress_user7 NOSUPERUSER NOCREATEDB"
            r" NOCREATEROLE INHERIT LOGIN NOREPLICATION BYPASSRLS;"
        ):
            createuser("--bypassrls", "regress_user7", server=pg)


def test_no_bypassrls(pg):
    with _drop_roles(pg, "regress_user8"):
        with pg.log_contains(
            r"statement: CREATE ROLE regress_user8 NOSUPERUSER NOCREATEDB"
            r" NOCREATEROLE INHERIT LOGIN NOREPLICATION NOBYPASSRLS;"
        ):
            createuser("--no-bypassrls", "regress_user8", server=pg)


def test_with_admin_single(pg):
    pg.sql("CREATE ROLE regress_user1 LOGIN")
    with _drop_roles(pg, "regress_user9", "regress_user1"):
        with pg.log_contains(
            r"statement: CREATE ROLE regress_user9 NOSUPERUSER NOCREATEDB"
            r" NOCREATEROLE INHERIT LOGIN NOREPLICATION NOBYPASSRLS"
            r" ADMIN regress_user1;"
        ):
            createuser("--with-admin", "regress_user1", "regress_user9", server=pg)


def test_with_member_single(pg):
    pg.sql("CREATE ROLE regress_user1 LOGIN")
    with _drop_roles(pg, "regress_user10", "regress_user1"):
        with pg.log_contains(
            r"statement: CREATE ROLE regress_user10 NOSUPERUSER NOCREATEDB"
            r" NOCREATEROLE INHERIT LOGIN NOREPLICATION NOBYPASSRLS"
            r" ROLE regress_user1;"
        ):
            createuser("--with-member", "regress_user1", "regress_user10", server=pg)


def test_role_in_role(pg):
    pg.sql("CREATE ROLE regress_user1 LOGIN")
    with _drop_roles(pg, "regress_user11", "regress_user1"):
        with pg.log_contains(
            r"statement: CREATE ROLE regress_user11 NOSUPERUSER NOCREATEDB"
            r" NOCREATEROLE INHERIT LOGIN NOREPLICATION NOBYPASSRLS"
            r" IN ROLE regress_user1;"
        ):
            createuser("--role", "regress_user1", "regress_user11", server=pg)


def test_member_of(pg):
    pg.sql("CREATE ROLE regress_user1 LOGIN")
    with _drop_roles(pg, "regress_user12", "regress_user1"):
        with pg.log_contains(
            r"statement: CREATE ROLE regress_user12 NOSUPERUSER NOCREATEDB"
            r" NOCREATEROLE INHERIT LOGIN NOREPLICATION NOBYPASSRLS"
            r" IN ROLE regress_user1;"
        ):
            createuser("regress_user12", "--member-of", "regress_user1", server=pg)


def test_fails_if_role_exists(pg):
    pg.sql("CREATE ROLE regress_user1 LOGIN")
    with _drop_roles(pg, "regress_user1"):
        r = createuser("regress_user1", server=pg, check=False)
        assert r.returncode != 0


def test_fails_too_many_non_options(pg):
    # Two bare role-name arguments (regress_user1 and regress_user3) is one too
    # many; createuser accepts only a single role name.
    r = createuser(
        "regress_user1",
        "--with-member",
        "regress_user2",
        "regress_user3",
        server=pg,
        check=False,
    )
    assert r.returncode != 0
