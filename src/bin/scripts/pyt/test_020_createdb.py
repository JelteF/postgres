# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/scripts/t/020_createdb.pl.

Exercises the createdb client program: the SQL it emits for various options
(encoding, template, strategy, owner, locale provider), the errors it reports
for bad input, and shared-dependency copying when cloning a template. ICU and
locale-provider cases that need a differently-initialized cluster run against
dedicated create_pg servers.
"""

import os

import pytest

from pypg.bins import createdb


def test_standard_options():
    createdb.check_standard_options()


def test_createdb_basic(pg):
    with pg.log_contains(r"statement: CREATE DATABASE foobar1"):
        createdb("foobar1", server=pg)
    pg.psql("-c", "DROP DATABASE foobar1")


def test_createdb_encoding(pg):
    # LATIN1 is compatible with the C locale used here, so template0 (which has
    # no datcollate/datctype restriction) can be cloned with this encoding.
    with pg.log_contains(r"statement: CREATE DATABASE foobar2 ENCODING 'LATIN1'"):
        createdb(
            "--locale",
            "C",
            "--encoding",
            "LATIN1",
            "--template",
            "template0",
            "foobar2",
            server=pg,
        )
    pg.psql("-c", "DROP DATABASE foobar2")


def test_createdb_already_exists(pg):
    pg.psql("-c", "CREATE DATABASE foobar1")
    # A second create of the same name must fail.
    createdb("foobar1", server=pg, check=False)
    r = createdb("foobar1", server=pg, check=False)
    assert r.returncode != 0
    pg.psql("-c", "DROP DATABASE foobar1")


def test_createdb_builtin_provider(pg):
    # The "builtin" locale provider requires a locale; without one createdb
    # fails, and the various combinations below succeed or fail depending on
    # whether the requested encoding/locale are mutually compatible.
    assert (
        createdb(
            "--template",
            "template0",
            "--locale-provider",
            "builtin",
            "tbuiltin1",
            server=pg,
            check=False,
        ).returncode
        != 0
    )

    createdb(
        "--template",
        "template0",
        "--locale-provider",
        "builtin",
        "--locale",
        "C",
        "tbuiltin2",
        server=pg,
    )
    createdb(
        "--template",
        "template0",
        "--locale-provider",
        "builtin",
        "--locale",
        "C",
        "--lc-collate",
        "C",
        "tbuiltin3",
        server=pg,
    )
    createdb(
        "--template",
        "template0",
        "--locale-provider",
        "builtin",
        "--locale",
        "C",
        "--lc-ctype",
        "C",
        "tbuiltin4",
        server=pg,
    )
    createdb(
        "--template",
        "template0",
        "--locale-provider",
        "builtin",
        "--lc-collate",
        "C",
        "--lc-ctype",
        "C",
        "--encoding",
        "UTF-8",
        "--builtin-locale",
        "C.UTF8",
        "tbuiltin5",
        server=pg,
    )

    # C.UTF-8 builtin locale is incompatible with a LATIN1 encoding.
    assert (
        createdb(
            "--template",
            "template0",
            "--locale-provider",
            "builtin",
            "--lc-collate",
            "C",
            "--lc-ctype",
            "C",
            "--encoding",
            "LATIN1",
            "--builtin-locale",
            "C.UTF-8",
            "tbuiltin6",
            server=pg,
            check=False,
        ).returncode
        != 0
    )

    # ICU_LOCALE / ICU_RULES are meaningless for the builtin provider.
    assert (
        createdb(
            "--template",
            "template0",
            "--locale-provider",
            "builtin",
            "--locale",
            "C",
            "--icu-locale",
            "en",
            "tbuiltin7",
            server=pg,
            check=False,
        ).returncode
        != 0
    )
    assert (
        createdb(
            "--template",
            "template0",
            "--locale-provider",
            "builtin",
            "--locale",
            "C",
            "--icu-rules",
            '""',
            "tbuiltin8",
            server=pg,
            check=False,
        ).returncode
        != 0
    )

    # template1 uses the libc provider, so a builtin-provider database can't be
    # cloned from it.
    assert (
        createdb(
            "--template",
            "template1",
            "--locale-provider",
            "builtin",
            "--locale",
            "C",
            "tbuiltin9",
            server=pg,
            check=False,
        ).returncode
        != 0
    )

    for db in ("tbuiltin2", "tbuiltin3", "tbuiltin4", "tbuiltin5"):
        pg.psql("-c", f"DROP DATABASE {db}")


def test_createdb_errors(pg):
    # Invalid locale provider name.
    assert (
        createdb(
            "--template",
            "template0",
            "--locale-provider",
            "xyz",
            "foobarX",
            server=pg,
            check=False,
        ).returncode
        != 0
    )

    # Newline / carriage return in the database name are rejected client-side.
    createdb.check_all(
        "invalid \n dbname",
        exit_code=1,
        stderr=r"contains a newline or carriage return character",
        server=pg,
    )
    createdb.check_all(
        "invalid \r dbname",
        exit_code=1,
        stderr=r"contains a newline or carriage return character",
        server=pg,
    )


def test_createdb_quote_handling(pg):
    # Option values that try to smuggle in SQL must be rejected, either by
    # createdb's own validation (encoding) or by the server (lc-collate,
    # lc-ctype, strategy).
    createdb.check_all(
        "--encoding",
        "foo'; SELECT '1",
        "foobarq",
        exit_code=1,
        stdout=r"^$",
        stderr=r"""^createdb: error: "foo'; SELECT '1" is not a valid encoding name""",
        server=pg,
    )
    createdb.check_all(
        "--lc-collate",
        "foo'; SELECT '1",
        "foobarq",
        exit_code=1,
        stdout=r"^$",
        stderr=(
            r"^createdb: error: database creation failed: ERROR:  invalid LC_COLLATE locale name"
            r"|^createdb: error: database creation failed: ERROR:  new collation \(foo'; SELECT '1\) is incompatible with the collation of the template database"
        ),
        server=pg,
    )
    createdb.check_all(
        "--lc-ctype",
        "foo'; SELECT '1",
        "foobarq",
        exit_code=1,
        stdout=r"^$",
        stderr=(
            r"^createdb: error: database creation failed: ERROR:  invalid LC_CTYPE locale name"
            r"|^createdb: error: database creation failed: ERROR:  new LC_CTYPE \(foo'; SELECT '1\) is incompatible with the LC_CTYPE of the template database"
        ),
        server=pg,
    )
    createdb.check_all(
        "--strategy",
        "foo",
        "foobarq",
        exit_code=1,
        stdout=r"^$",
        stderr=r'^createdb: error: database creation failed: ERROR:  invalid create database strategy "foo"',
        server=pg,
    )


def test_createdb_template_and_strategy(pg):
    # Build a template with shared dependencies (a role owning a table with a
    # policy) so that cloning it exercises copying pg_shdepend entries.
    pg.psql("-c", "CREATE DATABASE foobar2 LOCALE 'C' TEMPLATE template0")
    pg.psql(
        "foobar2",
        "-c",
        "CREATE ROLE role_foobar;"
        "CREATE TABLE tab_foobar (id int);"
        "ALTER TABLE tab_foobar owner to role_foobar;"
        "CREATE POLICY pol_foobar ON tab_foobar FOR ALL TO role_foobar;",
    )

    with pg.log_contains(
        r"statement: CREATE DATABASE foobar3 TEMPLATE foobar2 LOCALE 'C'"
    ):
        createdb("--locale", "C", "--template", "foobar2", "foobar3", server=pg)

    # The shared dependencies must have been copied into the cloned database.
    shdepend = pg.sql_oneshot(
        "SELECT pg_describe_object(classid, objid, objsubid) AS obj,"
        "       pg_describe_object(refclassid, refobjid, 0) AS refobj"
        "   FROM pg_shdepend s JOIN pg_database d ON (d.oid = s.dbid)"
        "   WHERE d.datname = 'foobar3' ORDER BY obj;",
        dbname="foobar3",
    )
    assert shdepend == [
        ("policy pol_foobar on table tab_foobar", "role role_foobar"),
        ("table tab_foobar", "role role_foobar"),
    ]

    # Database creation strategies: bareword vs. quoted (case-preserving) forms.
    with pg.log_contains(
        r"statement: CREATE DATABASE foobar6 STRATEGY wal_log TEMPLATE foobar2"
    ):
        createdb("--template", "foobar2", "--strategy", "wal_log", "foobar6", server=pg)
    with pg.log_contains(
        r'statement: CREATE DATABASE foobar6s STRATEGY "WAL_LOG" TEMPLATE foobar2'
    ):
        createdb(
            "--template", "foobar2", "--strategy", "WAL_LOG", "foobar6s", server=pg
        )
    with pg.log_contains(
        r"statement: CREATE DATABASE foobar7 STRATEGY file_copy TEMPLATE foobar2"
    ):
        createdb(
            "--template", "foobar2", "--strategy", "file_copy", "foobar7", server=pg
        )
    with pg.log_contains(
        r'statement: CREATE DATABASE foobar7s STRATEGY "FILE_COPY" TEMPLATE foobar2'
    ):
        createdb(
            "--template", "foobar2", "--strategy", "FILE_COPY", "foobar7s", server=pg
        )

    # Database owned by the role copied from the template.
    with pg.log_contains(
        r"statement: CREATE DATABASE foobar8 OWNER role_foobar TEMPLATE foobar2"
    ):
        createdb(
            "--template", "foobar2", "--owner", "role_foobar", "foobar8", server=pg
        )

    # foobar8 must be dropped before DROP OWNED BY can remove the role.
    pg.psql("foobar2", "-c", "DROP DATABASE foobar8")
    pg.psql("foobar2", "-c", "DROP OWNED BY role_foobar")

    for db in ("foobar3", "foobar6", "foobar6s", "foobar7", "foobar7s"):
        pg.psql("-c", f"DROP DATABASE {db}")
    pg.psql("foobar2", "-c", "DROP ROLE role_foobar")
    pg.psql("-c", "DROP DATABASE foobar2")


@pytest.mark.skipif(
    os.getenv("with_icu") != "yes", reason="ICU support not built (with_icu != yes)"
)
def test_createdb_icu_libc_template(pg):
    """ICU options against the default (libc-provider) template0 cluster.

    These run against the shared module server, whose template0 uses the libc
    provider and has no ICU locale set.
    """
    # Fails because template0 uses the libc provider and has no ICU locale set.
    assert (
        createdb(
            "--template",
            "template0",
            "--encoding",
            "UTF8",
            "--locale-provider",
            "icu",
            "foobar4",
            server=pg,
            check=False,
        ).returncode
        != 0
    )

    with pg.log_contains(
        r"statement: CREATE DATABASE foobar5 .* LOCALE_PROVIDER icu ICU_LOCALE 'en'"
    ):
        createdb(
            "--template",
            "template0",
            "--encoding",
            "UTF8",
            "--locale-provider",
            "icu",
            "--locale",
            "C",
            "--icu-locale",
            "en",
            "foobar5",
            server=pg,
        )
    pg.psql("-c", "DROP DATABASE foobar5")

    # Invalid ICU locale.
    assert (
        createdb(
            "--template",
            "template0",
            "--encoding",
            "UTF8",
            "--locale-provider",
            "icu",
            "--icu-locale",
            "@colNumeric=lower",
            "foobarX",
            server=pg,
            check=False,
        ).returncode
        != 0
    )

    # SQL_ASCII is not a valid encoding for the ICU provider.
    createdb.check_all(
        "--template",
        "template0",
        "--locale-provider",
        "icu",
        "--encoding",
        "SQL_ASCII",
        "foobarX",
        exit_code=1,
        stderr=r'ERROR:  encoding "SQL_ASCII" is not supported with ICU provider',
        server=pg,
    )


@pytest.mark.skipif(
    os.getenv("with_icu") != "yes", reason="ICU support not built (with_icu != yes)"
)
def test_createdb_icu_template(create_pg):
    """ICU options against a cluster whose template uses the ICU provider.

    Needs a dedicated cluster initialized with --locale-provider=icu at initdb
    time (the shared module server uses the libc provider), so it goes in its
    own create_pg server rather than the shared pg fixture.
    """
    node = create_pg("icu", initdb_opts=["--locale-provider=icu", "--icu-locale=en"])

    # libc provider explicitly requested from an ICU-provider template.
    node.psql("-c", "DROP DATABASE IF EXISTS foobar55")
    createdb(
        "--template",
        "template0",
        "--locale-provider",
        "libc",
        "foobar55",
        server=node,
    )
    # ICU locale carried over from the ICU-provider template.
    createdb(
        "--template",
        "template0",
        "--icu-locale",
        "en-US",
        "foobar56",
        server=node,
    )
    # --locale used as the ICU locale, with separate libc lc-collate/lc-ctype.
    createdb(
        "--template",
        "template0",
        "--locale-provider",
        "icu",
        "--locale",
        "en",
        "--lc-collate",
        "C",
        "--lc-ctype",
        "C",
        "foobar57",
        server=node,
    )
