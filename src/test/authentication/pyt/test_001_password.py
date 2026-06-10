# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/authentication/t/001_password.pl.

Exercises the password-based pg_hba.conf authentication methods (trust, plain
``password``, ``md5`` and ``scram-sha-256``), the ``require_auth`` connection
option matrix, the ``log_connections`` GUC, ``.pgpass`` processing, regular
expressions in the role/database columns of pg_hba.conf, and role-membership
matching (exact, ``+group``, ``samerole``, ``samegroup``).

This test can only run with Unix-domain sockets (the framework already uses
them on non-Windows platforms).

A few client-only differences from the Perl original:
 - ``connect_ok``/``connect_fails`` are expressed as ``pg.connect()`` either
   returning a connection or raising ``LibpqError``; failure messages are
   matched against the exception.
 - ``log_like``/``log_unlike`` are checked against the *server* log. The
   backend WARNING "role password will expire soon" is logged server-side, so
   it is asserted there rather than on client stderr. The purely client-side
   libpq notice "authenticated with an MD5-encrypted password" is not captured
   by the libpq wrapper (the notice receiver is only installed after connect),
   so that one stderr assertion is dropped; the equivalent server-side
   "connection authenticated ... method=md5" log line is checked instead.
 - The optional ``\\password`` interactive-psql sub-test is omitted: it needs a
   pseudo-terminal driver the framework does not have, and the Perl original
   already skips it unless IO::Pty is installed.
"""

import datetime
import re

import pytest

from libpq import LibpqError


def _check_log(pg, offset, like=(), unlike=()):
    for pat in like:
        pg.wait_for_log(pat, offset)
    log = pg.log_since(offset)
    for pat in unlike:
        assert not re.search(pat, log), f"unexpected match for {pat!r} in log"


def connect_ok(pg, *, like=(), unlike=(), **opts):
    """Mirror of Perl's connect_ok: the connection must succeed."""
    offset = pg.current_log_position()
    with pg.connect(**opts):
        pass
    _check_log(pg, offset, like=like, unlike=unlike)


def connect_fails(pg, *, match=None, unlike=(), **opts):
    """Mirror of Perl's connect_fails: the connection must raise LibpqError."""
    offset = pg.current_log_position()
    ctx = pytest.raises(LibpqError, match=match) if match else pytest.raises(LibpqError)
    with ctx:
        with pg.connect(**opts):
            pass
    _check_log(pg, offset, unlike=unlike)


def test_password(create_pg, monkeypatch):
    pg = create_pg(
        "primary",
        conf=[
            "log_connections = on",
            # Needed to allow connect_fails to inspect the postmaster log.
            "log_min_messages = debug2",
            "password_expiration_warning_threshold = '1100d'",
        ],
    )

    # Set up roles for the password_expiration_warning_threshold test.
    current_year = datetime.date.today().year
    pg.sql(
        f"CREATE ROLE expired LOGIN VALID UNTIL '{current_year - 1}-01-01' "
        "PASSWORD 'pass'"
    )
    pg.sql(
        "CREATE ROLE expiration_warnings LOGIN "
        f"VALID UNTIL '{current_year + 2}-01-01' PASSWORD 'pass'"
    )
    pg.sql(
        "CREATE ROLE no_warnings LOGIN "
        f"VALID UNTIL '{current_year + 5}-01-01' PASSWORD 'pass'"
    )

    # --- Behavior of the log_connections GUC ---
    #
    # There wasn't another test file where these tests obviously fit, and we
    # don't want to incur the cost of spinning up a new cluster just to test
    # one GUC. A dedicated database keeps the log assertions stable.
    pg.sql("CREATE DATABASE test_log_connections")

    assert pg.sql("SHOW log_connections", dbname="test_log_connections") == "on"

    connect_ok(
        pg,
        dbname="test_log_connections",
        like=[
            r"connection received",
            r"connection authenticated",
            r"connection authorized: user=\S+ database=test_log_connections",
        ],
        unlike=[r"connection ready"],
    )

    pg.sql("ALTER SYSTEM SET log_connections = 'receipt,authorization,setup_durations'")
    pg.sql("SELECT pg_reload_conf()")

    connect_ok(
        pg,
        dbname="test_log_connections",
        like=[
            r"connection received",
            r"connection authorized: user=\S+ database=test_log_connections",
            r"connection ready",
        ],
        unlike=[r"connection authenticated"],
    )

    pg.sql("ALTER SYSTEM SET log_connections = 'all'")
    pg.sql("SELECT pg_reload_conf()")

    connect_ok(
        pg,
        dbname="test_log_connections",
        like=[
            r"connection received",
            r"connection authenticated",
            r"connection authorized: user=\S+ database=test_log_connections",
            r"connection ready",
        ],
    )

    # --- Authentication tests ---

    # md5 could fail in FIPS mode.
    try:
        pg.sql("select md5('')")
        md5_works = True
    except LibpqError:
        md5_works = False

    # Create roles with different password methods; the same password is used
    # for all of them.
    pg.sql(
        "SET password_encryption='scram-sha-256'; "
        "CREATE ROLE scram_role LOGIN PASSWORD 'pass';"
    )
    if md5_works:
        pg.sql(
            "SET password_encryption='md5'; "
            "CREATE ROLE md5_role LOGIN PASSWORD 'pass';"
        )

    # Set up a table for tests of SYSTEM_USER.
    pg.sql(
        "CREATE TABLE sysuser_data (n) AS SELECT NULL FROM generate_series(1, 10);"
        " GRANT ALL ON sysuser_data TO scram_role;"
    )
    monkeypatch.setenv("PGPASSWORD", "pass")

    # Create a role that contains a comma to stress the parsing.
    pg.sql(
        "SET password_encryption='scram-sha-256'; "
        'CREATE ROLE "scram,role" LOGIN PASSWORD \'pass\';'
    )

    # Create a role with a non-default iteration count.
    pg.sql(
        "SET password_encryption='scram-sha-256';"
        " SET scram_iterations=1024;"
        " CREATE ROLE scram_role_iter LOGIN PASSWORD 'pass';"
        " RESET scram_iterations;"
    )

    res = pg.sql(
        "SELECT substr(rolpassword,1,19) FROM pg_authid "
        "WHERE rolname = 'scram_role_iter'"
    )
    assert res == "SCRAM-SHA-256$1024:", "scram_iterations in server side ROLE"

    # NB: the Perl test here also drives psql's \password command through an
    # interactive pseudo-terminal to confirm client-side scram_iterations. That
    # needs a pty helper the framework lacks (and Perl skips it without IO::Pty),
    # so it is omitted.

    # Create a database to test regular expressions.
    pg.sql("CREATE database regex_testdb;")

    # For "trust" method, all users should be able to connect.
    pg.reset_hba("all", "all", "trust")
    connect_ok(
        pg,
        user="scram_role",
        like=[r'connection authenticated: user="scram_role" method=trust'],
    )
    if md5_works:
        connect_ok(
            pg,
            user="md5_role",
            like=[r'connection authenticated: user="md5_role" method=trust'],
        )

    # SYSTEM_USER is null when not authenticated.
    assert pg.sql("SELECT SYSTEM_USER IS NULL;") is True, (
        "users with trust authentication use SYSTEM_USER = NULL"
    )

    # Test SYSTEM_USER with parallel workers when not authenticated.
    with pg.connect(user="scram_role") as c:
        res = c.sql(
            "SET min_parallel_table_scan_size TO 0;"
            " SET parallel_setup_cost TO 0;"
            " SET parallel_tuple_cost TO 0;"
            " SET max_parallel_workers_per_gather TO 2;"
            " SELECT bool_and(SYSTEM_USER IS NOT DISTINCT FROM n) FROM sysuser_data;"
        )
    assert res is True, (
        "users with trust authentication use SYSTEM_USER = NULL in parallel workers"
    )

    # Explicitly specifying an empty require_auth (the default) should always
    # succeed.
    connect_ok(pg, user="scram_role", require_auth="")

    # All these values of require_auth should fail, as trust is expected.
    for method in [
        "gss",
        "sspi",
        "password",
        "md5",
        "scram-sha-256",
        "password,scram-sha-256",
    ]:
        connect_fails(
            pg,
            user="scram_role",
            require_auth=method,
            match=rf'authentication method requirement "{re.escape(method)}" failed: '
            r"server did not complete authentication",
        )

    # These negative patterns of require_auth should succeed.
    for method in [
        "!gss",
        "!sspi",
        "!password",
        "!md5",
        "!scram-sha-256",
        "!password,!scram-sha-256",
    ]:
        connect_ok(pg, user="scram_role", require_auth=method)

    # require_auth=[!]none should interact correctly with trust auth.
    connect_ok(pg, user="scram_role", require_auth="none")
    connect_fails(
        pg,
        user="scram_role",
        require_auth="!none",
        match=r"server did not complete authentication",
    )

    # Negative and positive require_auth methods can't be mixed.
    connect_fails(
        pg,
        user="scram_role",
        require_auth="scram-sha-256,!md5",
        match=r'negative require_auth method "!md5" cannot be mixed with '
        r"non-negative methods",
    )
    connect_fails(
        pg,
        user="scram_role",
        require_auth="!password,!none,scram-sha-256",
        match=r'require_auth method "scram-sha-256" cannot be mixed with '
        r"negative methods",
    )

    # require_auth methods cannot have duplicated values.
    for method, dup in [
        ("password,md5,password", "password"),
        ("!password,!md5,!password", "!password"),
        ("none,md5,none", "none"),
        ("!none,!md5,!none", "!none"),
        ("scram-sha-256,scram-sha-256", "scram-sha-256"),
        ("!scram-sha-256,!scram-sha-256", "!scram-sha-256"),
    ]:
        connect_fails(
            pg,
            user="scram_role",
            require_auth=method,
            match=rf'require_auth method "{re.escape(dup)}" is specified more than once',
        )

    # Unknown value defined in require_auth.
    connect_fails(
        pg,
        user="scram_role",
        require_auth="none,abcdefg",
        match=r'invalid require_auth value: "abcdefg"',
    )

    # For plain "password" method, all users should also be able to connect.
    pg.reset_hba("all", "all", "password")
    connect_ok(
        pg,
        user="scram_role",
        like=[r'connection authenticated: identity="scram_role" method=password'],
    )
    if md5_works:
        connect_ok(
            pg,
            user="md5_role",
            like=[r'connection authenticated: identity="md5_role" method=password'],
        )

    # require_auth succeeds here with a plaintext password.
    connect_ok(pg, user="scram_role", require_auth="password")
    connect_ok(pg, user="scram_role", require_auth="!none")
    connect_ok(pg, user="scram_role", require_auth="scram-sha-256,password,md5")

    # require_auth fails for other authentication types.
    connect_fails(
        pg,
        user="scram_role",
        require_auth="md5",
        match=r'authentication method requirement "md5" failed: '
        r"server requested a cleartext password",
    )
    connect_fails(
        pg,
        user="scram_role",
        require_auth="scram-sha-256",
        match=r'authentication method requirement "scram-sha-256" failed: '
        r"server requested a cleartext password",
    )
    connect_fails(
        pg,
        user="scram_role",
        require_auth="none",
        match=r'authentication method requirement "none" failed: '
        r"server requested a cleartext password",
    )

    # Disallowing password authentication fails, even if requested by server.
    connect_fails(
        pg,
        user="scram_role",
        require_auth="!password",
        match=r"server requested a cleartext password",
    )
    connect_fails(
        pg,
        user="scram_role",
        require_auth="!password,!md5,!scram-sha-256",
        match=r' method requirement "!password,!md5,!scram-sha-256" failed: '
        r"server requested a cleartext password",
    )

    # For "scram-sha-256" method, user "scram_role" should be able to connect.
    pg.reset_hba("all", "all", "scram-sha-256")
    connect_ok(
        pg,
        user="scram_role",
        like=[r'connection authenticated: identity="scram_role" method=scram-sha-256'],
    )
    connect_ok(
        pg,
        user="scram_role_iter",
        like=[
            r'connection authenticated: identity="scram_role_iter" method=scram-sha-256'
        ],
    )
    connect_fails(pg, user="md5_role", unlike=[r"connection authenticated:"])

    # require_auth should succeed with SCRAM when it is required.
    connect_ok(pg, user="scram_role", require_auth="scram-sha-256")
    connect_ok(pg, user="scram_role", require_auth="!none")
    connect_ok(pg, user="scram_role", require_auth="password,scram-sha-256,md5")

    # Authentication fails for other authentication types.
    connect_fails(
        pg,
        user="scram_role",
        require_auth="password",
        match=r'authentication method requirement "password" failed: '
        r"server requested SASL authentication",
    )
    connect_fails(
        pg,
        user="scram_role",
        require_auth="md5",
        match=r'authentication method requirement "md5" failed: '
        r"server requested SASL authentication",
    )
    connect_fails(
        pg,
        user="scram_role",
        require_auth="none",
        match=r'authentication method requirement "none" failed: '
        r"server requested SASL authentication",
    )

    # Authentication fails if SCRAM authentication is forbidden.
    connect_fails(
        pg,
        user="scram_role",
        require_auth="!scram-sha-256",
        match=r"server requested SCRAM-SHA-256 authentication",
    )
    connect_fails(
        pg,
        user="scram_role",
        require_auth="!password,!md5,!scram-sha-256",
        match=r"server requested SCRAM-SHA-256 authentication",
    )

    # Test that bad passwords are rejected.
    monkeypatch.setenv("PGPASSWORD", "badpass")
    connect_fails(pg, user="scram_role", unlike=[r"connection authenticated:"])
    monkeypatch.setenv("PGPASSWORD", "pass")

    # For "md5" method, all users should be able to connect (SCRAM
    # authentication will be performed for the user with a SCRAM secret).
    pg.reset_hba("all", "all", "md5")
    connect_ok(
        pg,
        user="scram_role",
        like=[r'connection authenticated: identity="scram_role" method=md5'],
    )
    if md5_works:
        # The Perl test also checks the client-side notice "authenticated with
        # an MD5-encrypted password"; that libpq message is not captured by the
        # wrapper, so only the server-side log line is asserted.
        connect_ok(
            pg,
            user="md5_role",
            like=[r'connection authenticated: identity="md5_role" method=md5'],
        )

    # require_auth succeeds with SCRAM required.
    connect_ok(pg, user="scram_role", require_auth="scram-sha-256")
    connect_ok(pg, user="scram_role", require_auth="!none")
    connect_ok(pg, user="scram_role", require_auth="md5,scram-sha-256,password")

    # Authentication fails if other types are required.
    connect_fails(
        pg,
        user="scram_role",
        require_auth="password",
        match=r'authentication method requirement "password" failed: '
        r"server requested SASL authentication",
    )
    connect_fails(
        pg,
        user="scram_role",
        require_auth="md5",
        match=r'authentication method requirement "md5" failed: '
        r"server requested SASL authentication",
    )
    connect_fails(
        pg,
        user="scram_role",
        require_auth="none",
        match=r'authentication method requirement "none" failed: '
        r"server requested SASL authentication",
    )

    # Authentication fails if SCRAM is forbidden.
    connect_fails(
        pg,
        user="scram_role",
        require_auth="!scram-sha-256",
        match=r'authentication method requirement "!scram-sha-256" failed: '
        r"server requested SCRAM-SHA-256 authentication",
    )
    connect_fails(
        pg,
        user="scram_role",
        require_auth="!password,!md5,!scram-sha-256",
        match=r'authentication method requirement "!password,!md5,!scram-sha-256" '
        r"failed: server requested SCRAM-SHA-256 authentication",
    )

    # Test password_expiration_warning_threshold.
    connect_fails(
        pg,
        user="expired",
        dbname="postgres",
        match=r'password authentication failed for user "expired"',
    )
    connect_ok(
        pg,
        user="expiration_warnings",
        dbname="postgres",
        like=[r"role password will expire soon"],
    )
    connect_ok(pg, user="no_warnings", dbname="postgres")

    # Test SYSTEM_USER <> NULL with parallel workers.
    with pg.connect(user="scram_role") as c:
        c.sql(
            "TRUNCATE sysuser_data;"
            " INSERT INTO sysuser_data SELECT 'md5:scram_role' "
            "FROM generate_series(1, 10);"
        )
        res = c.sql(
            "SET min_parallel_table_scan_size TO 0;"
            " SET parallel_setup_cost TO 0;"
            " SET parallel_tuple_cost TO 0;"
            " SET max_parallel_workers_per_gather TO 2;"
            " SELECT bool_and(SYSTEM_USER IS NOT DISTINCT FROM n) FROM sysuser_data;"
        )
    assert res is True, (
        "users with md5 authentication use SYSTEM_USER = md5:role in parallel workers"
    )

    # --- Tests for channel binding without SSL ---
    # Using the password authentication method; channel binding can't work.
    pg.reset_hba("all", "all", "password")
    monkeypatch.setenv("PGCHANNELBINDING", "require")
    connect_fails(pg, user="scram_role")
    # SSL not in use; channel binding still can't work.
    pg.reset_hba("all", "all", "scram-sha-256")
    monkeypatch.setenv("PGCHANNELBINDING", "require")
    connect_fails(pg, user="scram_role")

    # --- Test .pgpass processing; use a temp file, don't touch the real one ---
    pgpassfile = pg.datadir.parent / "pgpass"

    monkeypatch.delenv("PGPASSWORD")
    monkeypatch.delenv("PGCHANNELBINDING")
    monkeypatch.setenv("PGPASSFILE", str(pgpassfile))

    long_comment = (
        "# This very long comment is just here to exercise handling of long "
        "lines in the file. " * 5
    )
    pgpassfile.write_text(
        "\n"
        + long_comment
        + "\n"
        + "*:*:postgres:scram_role:pass:this is not part of the password.\n"
    )
    pgpassfile.chmod(0o600)

    pg.reset_hba("all", "all", "password")
    connect_ok(pg, user="scram_role")
    connect_fails(pg, user="md5_role")

    with open(pgpassfile, "a") as f:
        f.write("\n*:*:*:scram_role:p\\ass\n*:*:*:scram,role:p\\ass\n")

    connect_ok(pg, user="scram_role")

    # Testing with regular expression for username. The third regexp matches.
    pg.reset_hba("all", "/^.*nomatch.*$, baduser, /^scr.*$", "password")
    connect_ok(
        pg,
        user="scram_role",
        like=[r'connection authenticated: identity="scram_role" method=password'],
    )

    # The third regex does not match anymore.
    pg.reset_hba("all", "/^.*nomatch.*$, baduser, /^sc_r.*$", "password")
    connect_fails(pg, user="scram_role", unlike=[r"connection authenticated:"])

    # Test with a comma in the regular expression. In this case, the use of
    # double quotes is mandatory so this is not considered as two elements of
    # the user name list when parsing pg_hba.conf.
    pg.reset_hba("all", '"/^.*m,.*e$"', "password")
    connect_ok(
        pg,
        user="scram,role",
        like=[r'connection authenticated: identity="scram,role" method=password'],
    )

    # Testing with regular expression for dbname. The third regex matches.
    pg.reset_hba("/^.*nomatch.*$, baddb, /^regex_t.*b$", "all", "password")
    connect_ok(
        pg,
        user="scram_role",
        dbname="regex_testdb",
        like=[r'connection authenticated: identity="scram_role" method=password'],
    )

    # The third regexp does not match anymore.
    pg.reset_hba("/^.*nomatch.*$, baddb, /^regex_t.*ba$", "all", "password")
    connect_fails(
        pg,
        user="scram_role",
        dbname="regex_testdb",
        unlike=[r"connection authenticated:"],
    )

    pgpassfile.unlink()
    monkeypatch.delenv("PGPASSFILE")

    # --- Authentication tests with specific HBA policies on roles ---

    # Create database and roles for membership tests.
    pg.reset_hba("all", "all", "trust")
    # Database and root role names match for "samerole" and "samegroup".
    pg.sql("CREATE DATABASE regress_regression_group;")
    pg.sql(
        "CREATE ROLE regress_regression_group LOGIN PASSWORD 'pass';"
        " CREATE ROLE regress_member LOGIN SUPERUSER IN ROLE "
        "regress_regression_group PASSWORD 'pass';"
        " CREATE ROLE regress_not_member LOGIN SUPERUSER PASSWORD 'pass';"
    )

    monkeypatch.setenv("PGPASSWORD", "pass")

    # Test role with exact matching, no members allowed.
    pg.reset_hba("all", "regress_regression_group", "scram-sha-256")
    connect_ok(
        pg,
        user="regress_regression_group",
        like=[
            r'connection authenticated: identity="regress_regression_group" '
            r"method=scram-sha-256"
        ],
    )
    connect_fails(
        pg,
        user="regress_member",
        unlike=[
            r'connection authenticated: identity="regress_member" '
            r"method=scram-sha-256"
        ],
    )
    connect_fails(
        pg,
        user="regress_not_member",
        unlike=[
            r'connection authenticated: identity="regress_not_member" '
            r"method=scram-sha-256"
        ],
    )

    # Test role membership with '+', where all the members are allowed to
    # connect.
    pg.reset_hba("all", "+regress_regression_group", "scram-sha-256")
    connect_ok(
        pg,
        user="regress_regression_group",
        like=[
            r'connection authenticated: identity="regress_regression_group" '
            r"method=scram-sha-256"
        ],
    )
    connect_ok(
        pg,
        user="regress_member",
        like=[
            r'connection authenticated: identity="regress_member" '
            r"method=scram-sha-256"
        ],
    )
    connect_fails(
        pg,
        user="regress_not_member",
        unlike=[
            r'connection authenticated: identity="regress_not_member" '
            r"method=scram-sha-256"
        ],
    )

    # Test role membership is respected for samerole.
    pg.reset_hba("samerole", "all", "scram-sha-256")
    connect_ok(
        pg,
        user="regress_regression_group",
        dbname="regress_regression_group",
        like=[
            r'connection authenticated: identity="regress_regression_group" '
            r"method=scram-sha-256"
        ],
    )
    connect_ok(
        pg,
        user="regress_member",
        dbname="regress_regression_group",
        like=[
            r'connection authenticated: identity="regress_member" '
            r"method=scram-sha-256"
        ],
    )
    connect_fails(
        pg,
        user="regress_not_member",
        dbname="regress_regression_group",
        unlike=[
            r'connection authenticated: identity="regress_not_member" '
            r"method=scram-sha-256"
        ],
    )

    # Test role membership is respected for samegroup.
    pg.reset_hba("samegroup", "all", "scram-sha-256")
    connect_ok(
        pg,
        user="regress_regression_group",
        dbname="regress_regression_group",
        like=[
            r'connection authenticated: identity="regress_regression_group" '
            r"method=scram-sha-256"
        ],
    )
    connect_ok(
        pg,
        user="regress_member",
        dbname="regress_regression_group",
        like=[
            r'connection authenticated: identity="regress_member" '
            r"method=scram-sha-256"
        ],
    )
    connect_fails(
        pg,
        user="regress_not_member",
        dbname="regress_regression_group",
        unlike=[
            r'connection authenticated: identity="regress_not_member" '
            r"method=scram-sha-256"
        ],
    )
