# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/scripts/t/020_createdb.pl."""

import os

import pytest

WITH_ICU = os.environ.get("with_icu") == "yes"


def test_standard_options(pg_bin):
    pg_bin.check_help("createdb")
    pg_bin.check_version("createdb")
    pg_bin.check_bad_option("createdb")


def test_createdb(node, pg_bin, sql_like):
    sql_like(node, ["createdb", "foobar1"], r"statement: CREATE DATABASE foobar1")
    sql_like(
        node,
        [
            "createdb",
            "--locale", "C",
            "--encoding", "LATIN1",
            "--template", "template0",
            "foobar2",
        ],
        r"statement: CREATE DATABASE foobar2 ENCODING 'LATIN1'",
    )

    if not WITH_ICU:
        assert (
            pg_bin.run(
                "createdb",
                "--template", "template0",
                "--locale-provider", "icu",
                "foobar4",
                server=node,
            ).returncode
            != 0
        )

    assert (
        pg_bin.run(
            "createdb",
            "--template", "template0",
            "--locale-provider", "builtin",
            "tbuiltin1",
            server=node,
        ).returncode
        != 0
    ), "provider builtin fails without --locale"

    for db, args in [
        ("tbuiltin2", ["--locale", "C"]),
        ("tbuiltin3", ["--locale", "C", "--lc-collate", "C"]),
        ("tbuiltin4", ["--locale", "C", "--lc-ctype", "C"]),
        (
            "tbuiltin5",
            ["--lc-collate", "C", "--lc-ctype", "C", "--encoding", "UTF-8",
             "--builtin-locale", "C.UTF8"],
        ),
    ]:
        r = pg_bin.run(
            "createdb", "--template", "template0",
            "--locale-provider", "builtin", *args, db, server=node,
        )
        assert r.returncode == 0, r.stderr

    for args in [
        ["--lc-collate", "C", "--lc-ctype", "C", "--encoding", "LATIN1",
         "--builtin-locale", "C.UTF-8", "tbuiltin6"],
        ["--locale", "C", "--icu-locale", "en", "tbuiltin7"],
        ["--locale", "C", "--icu-rules", '""', "tbuiltin8"],
    ]:
        assert (
            pg_bin.run(
                "createdb", "--template", "template0",
                "--locale-provider", "builtin", *args, server=node,
            ).returncode
            != 0
        )
    assert (
        pg_bin.run(
            "createdb", "--template", "template1",
            "--locale-provider", "builtin", "--locale", "C", "tbuiltin9",
            server=node,
        ).returncode
        != 0
    ), "builtin not matching template"

    assert pg_bin.run("createdb", "foobar1", server=node).returncode != 0, \
        "fails if database already exists"

    assert (
        pg_bin.run(
            "createdb", "--template", "template0",
            "--locale-provider", "xyz", "foobarX", server=node,
        ).returncode
        != 0
    ), "invalid locale provider"

    pg_bin.check_all(
        "createdb", "invalid \n dbname", exit_code=1, server=node,
        stderr=[r"contains a newline or carriage return character"],
    )
    pg_bin.check_all(
        "createdb", "invalid \r dbname", exit_code=1, server=node,
        stderr=[r"contains a newline or carriage return character"],
    )

    # Check use of templates with shared dependencies copied from the template.
    # The connection must be closed before foobar2 is used as a template,
    # otherwise CREATE DATABASE rejects it as "being accessed by other users".
    with node.connect(dbname="foobar2") as foobar2:
        foobar2.sql(
            "CREATE ROLE role_foobar;"
            " CREATE TABLE tab_foobar (id int);"
            " ALTER TABLE tab_foobar owner to role_foobar;"
            " CREATE POLICY pol_foobar ON tab_foobar FOR ALL TO role_foobar;"
        )
    sql_like(
        node,
        ["createdb", "--locale", "C", "--template", "foobar2", "foobar3"],
        r"statement: CREATE DATABASE foobar3 TEMPLATE foobar2 LOCALE 'C'",
    )
    with node.connect(dbname="foobar3") as foobar3:
        shdeps = foobar3.sql(
            "SELECT pg_describe_object(classid, objid, objsubid) AS obj,"
            " pg_describe_object(refclassid, refobjid, 0) AS refobj"
            " FROM pg_shdepend s JOIN pg_database d ON (d.oid = s.dbid)"
            " WHERE d.datname = 'foobar3' ORDER BY obj;"
        )
    assert shdeps == [
        ("policy pol_foobar on table tab_foobar", "role role_foobar"),
        ("table tab_foobar", "role role_foobar"),
    ]

    # Check quote handling with incorrect option values.
    pg_bin.check_all(
        "createdb", "--encoding", "foo'; SELECT '1", "foobar2",
        exit_code=1, server=node, stdout=[r"^$"],
        stderr=[r"^createdb: error: \"foo'; SELECT '1\" is not a valid encoding name"],
    )
    pg_bin.check_all(
        "createdb", "--lc-collate", "foo'; SELECT '1", "foobar2",
        exit_code=1, server=node, stdout=[r"^$"],
        stderr=[
            r"^createdb: error: database creation failed: ERROR:  invalid LC_COLLATE locale name"
            r"|^createdb: error: database creation failed: ERROR:  new collation \(foo'; SELECT '1\) is incompatible with the collation of the template database"
        ],
    )
    pg_bin.check_all(
        "createdb", "--lc-ctype", "foo'; SELECT '1", "foobar2",
        exit_code=1, server=node, stdout=[r"^$"],
        stderr=[
            r"^createdb: error: database creation failed: ERROR:  invalid LC_CTYPE locale name"
            r"|^createdb: error: database creation failed: ERROR:  new LC_CTYPE \(foo'; SELECT '1\) is incompatible with the LC_CTYPE of the template database"
        ],
    )
    pg_bin.check_all(
        "createdb", "--strategy", "foo", "foobar2",
        exit_code=1, server=node, stdout=[r"^$"],
        stderr=[r"^createdb: error: database creation failed: ERROR:  invalid create database strategy \"foo\""],
    )

    # Check database creation strategy
    sql_like(
        node,
        ["createdb", "--template", "foobar2", "--strategy", "wal_log", "foobar6"],
        r"statement: CREATE DATABASE foobar6 STRATEGY wal_log TEMPLATE foobar2",
    )
    sql_like(
        node,
        ["createdb", "--template", "foobar2", "--strategy", "WAL_LOG", "foobar6s"],
        r'statement: CREATE DATABASE foobar6s STRATEGY "WAL_LOG" TEMPLATE foobar2',
    )
    sql_like(
        node,
        ["createdb", "--template", "foobar2", "--strategy", "file_copy", "foobar7"],
        r"statement: CREATE DATABASE foobar7 STRATEGY file_copy TEMPLATE foobar2",
    )
    sql_like(
        node,
        ["createdb", "--template", "foobar2", "--strategy", "FILE_COPY", "foobar7s"],
        r'statement: CREATE DATABASE foobar7s STRATEGY "FILE_COPY" TEMPLATE foobar2',
    )

    # Create database owned by role_foobar.
    sql_like(
        node,
        ["createdb", "--template", "foobar2", "--owner", "role_foobar", "foobar8"],
        r"statement: CREATE DATABASE foobar8 OWNER role_foobar TEMPLATE foobar2",
    )
    with node.connect(dbname="foobar2") as foobar2:
        foobar2.sql("DROP OWNED BY role_foobar;")
        foobar2.sql("DROP DATABASE foobar8;")


@pytest.mark.skipif(not WITH_ICU, reason="ICU support not built")
def test_createdb_icu(node, pg_bin, sql_like, create_pg):
    # This fails because template0 uses libc provider and has no ICU locale.
    assert (
        pg_bin.run(
            "createdb",
            "--template", "template0",
            "--encoding", "UTF8",
            "--locale-provider", "icu",
            "foobar4",
            server=node,
        ).returncode
        != 0
    )
    sql_like(
        node,
        [
            "createdb",
            "--template", "template0",
            "--encoding", "UTF8",
            "--locale-provider", "icu",
            "--locale", "C",
            "--icu-locale", "en",
            "foobar5",
        ],
        r"statement: CREATE DATABASE foobar5 .* LOCALE_PROVIDER icu ICU_LOCALE 'en'",
    )
    assert (
        pg_bin.run(
            "createdb",
            "--template", "template0",
            "--encoding", "UTF8",
            "--locale-provider", "icu",
            "--icu-locale", "@colNumeric=lower",
            "foobarX",
            server=node,
        ).returncode
        != 0
    ), "invalid ICU locale"
    pg_bin.check_all(
        "createdb",
        "--template", "template0",
        "--locale-provider", "icu",
        "--encoding", "SQL_ASCII",
        "foobarX",
        exit_code=1, server=node,
        stderr=[r"ERROR:  encoding \"SQL_ASCII\" is not supported with ICU provider"],
    )

    # additional node, which uses the icu provider
    node2 = create_pg("createdb_icu", initdb_opts=["--locale-provider=icu", "--icu-locale=en"])
    for args in [
        ["--locale-provider", "libc", "foobar55"],
        ["--icu-locale", "en-US", "foobar56"],
        ["--locale-provider", "icu", "--locale", "en", "--lc-collate", "C",
         "--lc-ctype", "C", "foobar57"],
    ]:
        r = pg_bin.run("createdb", "--template", "template0", *args, server=node2)
        assert r.returncode == 0, r.stderr
