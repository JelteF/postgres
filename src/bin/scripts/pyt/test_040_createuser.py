# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/scripts/t/040_createuser.pl."""


def test_standard_options(pg_bin):
    pg_bin.check_help("createuser")
    pg_bin.check_version("createuser")
    pg_bin.check_bad_option("createuser")


def test_createuser(node, pg_bin, sql_like):
    sql_like(
        node,
        ["createuser", "regress_user1"],
        r"statement: CREATE ROLE regress_user1 NOSUPERUSER NOCREATEDB NOCREATEROLE"
        r" INHERIT LOGIN NOREPLICATION NOBYPASSRLS;",
    )
    sql_like(
        node,
        ["createuser", "--no-login", "regress_role1"],
        r"statement: CREATE ROLE regress_role1 NOSUPERUSER NOCREATEDB NOCREATEROLE"
        r" INHERIT NOLOGIN NOREPLICATION NOBYPASSRLS;",
    )
    sql_like(
        node,
        ["createuser", "--createrole", "regress user2"],
        r'statement: CREATE ROLE "regress user2" NOSUPERUSER NOCREATEDB CREATEROLE'
        r" INHERIT LOGIN NOREPLICATION NOBYPASSRLS;",
    )
    sql_like(
        node,
        ["createuser", "--superuser", "regress_user3"],
        r"statement: CREATE ROLE regress_user3 SUPERUSER CREATEDB CREATEROLE"
        r" INHERIT LOGIN NOREPLICATION NOBYPASSRLS;",
    )
    sql_like(
        node,
        [
            "createuser",
            "--with-admin", "regress_user1",
            "--with-admin", "regress user2",
            "regress user #4",
        ],
        r'statement: CREATE ROLE "regress user #4" NOSUPERUSER NOCREATEDB NOCREATEROLE'
        r' INHERIT LOGIN NOREPLICATION NOBYPASSRLS ADMIN regress_user1,"regress user2";',
    )
    sql_like(
        node,
        [
            "createuser",
            "REGRESS_USER5",
            "--with-member", "regress_user3",
            "--with-member", "regress user #4",
        ],
        r'statement: CREATE ROLE "REGRESS_USER5" NOSUPERUSER NOCREATEDB NOCREATEROLE'
        r' INHERIT LOGIN NOREPLICATION NOBYPASSRLS ROLE regress_user3,"regress user #4";',
    )
    sql_like(
        node,
        ["createuser", "--valid-until", "2029 12 31", "regress_user6"],
        r"statement: CREATE ROLE regress_user6 NOSUPERUSER NOCREATEDB NOCREATEROLE"
        r" INHERIT LOGIN NOREPLICATION NOBYPASSRLS VALID UNTIL '2029 12 31';",
    )
    sql_like(
        node,
        ["createuser", "--bypassrls", "regress_user7"],
        r"statement: CREATE ROLE regress_user7 NOSUPERUSER NOCREATEDB NOCREATEROLE"
        r" INHERIT LOGIN NOREPLICATION BYPASSRLS;",
    )
    sql_like(
        node,
        ["createuser", "--no-bypassrls", "regress_user8"],
        r"statement: CREATE ROLE regress_user8 NOSUPERUSER NOCREATEDB NOCREATEROLE"
        r" INHERIT LOGIN NOREPLICATION NOBYPASSRLS;",
    )
    sql_like(
        node,
        ["createuser", "--with-admin", "regress_user1", "regress_user9"],
        r"statement: CREATE ROLE regress_user9 NOSUPERUSER NOCREATEDB NOCREATEROLE"
        r" INHERIT LOGIN NOREPLICATION NOBYPASSRLS ADMIN regress_user1;",
    )
    sql_like(
        node,
        ["createuser", "--with-member", "regress_user1", "regress_user10"],
        r"statement: CREATE ROLE regress_user10 NOSUPERUSER NOCREATEDB NOCREATEROLE"
        r" INHERIT LOGIN NOREPLICATION NOBYPASSRLS ROLE regress_user1;",
    )
    sql_like(
        node,
        ["createuser", "--role", "regress_user1", "regress_user11"],
        r"statement: CREATE ROLE regress_user11 NOSUPERUSER NOCREATEDB NOCREATEROLE"
        r" INHERIT LOGIN NOREPLICATION NOBYPASSRLS IN ROLE regress_user1;",
    )
    sql_like(
        node,
        ["createuser", "regress_user12", "--member-of", "regress_user1"],
        r"statement: CREATE ROLE regress_user12 NOSUPERUSER NOCREATEDB NOCREATEROLE"
        r" INHERIT LOGIN NOREPLICATION NOBYPASSRLS IN ROLE regress_user1;",
    )

    assert pg_bin.run("createuser", "regress_user1", server=node).returncode != 0
    assert (
        pg_bin.run(
            "createuser",
            "regress_user1",
            "--with-member", "regress_user2",
            "regress_user3",
            server=node,
        ).returncode
        != 0
    )
