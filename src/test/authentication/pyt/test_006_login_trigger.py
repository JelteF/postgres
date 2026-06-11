# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/authentication/t/006_login_trigger.pl.

Tests authentication via a login event trigger, mostly the rejection-via-
exception path that cannot be covered with regress *.sql/*.out tests. A login
trigger logs every connection into a table and emits a NOTICE ("You are
welcome!"), so each connection must be a fresh psql process: every command is
run with ``psql`` (a separate connection that re-fires the trigger), and its
stdout (query output) and stderr (the NOTICE) are checked.

psql is used rather than the libpq wrapper because the login-trigger NOTICE is
delivered during connection startup, before the wrapper installs its notice
receiver, so it would otherwise be lost.

This test can only run with Unix-domain sockets (the framework already uses
them on non-Windows platforms).
"""

import re
import sys

import pytest

# This test connects over Unix-domain sockets, which the framework only uses on
# non-Windows platforms.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="requires Unix-domain sockets"
)


def test_login_trigger(create_pg, pg_bin):
    node = create_pg(
        "main",
        initdb_opts=["--locale=C", "--encoding=UTF8"],
        conf=[
            "wal_level = 'logical'",
            "max_replication_slots = 4",
            "max_wal_senders = 4",
        ],
    )

    # Supplying initdb options forces a real initdb, which defaults local auth
    # to peer; the Perl test relies on trust so it can connect as other roles
    # (e.g. regress_alice). Reload with a trust rule to match that.
    node.reset_hba("all", "all", "trust")

    def psql_command(
        sql,
        *,
        connstr="postgres",
        exit_code=0,
        out_exact=None,
        out_like=(),
        out_unlike=(),
        err_exact=None,
        err_like=(),
        err_unlike=(),
    ):
        r = pg_bin.run("psql", "-X", "-A", "-t", "-q", "-d", connstr, "-c", sql,
                       server=node)
        assert r.returncode == exit_code, (
            f"expected exit {exit_code}, got {r.returncode}\n"
            f"stdout: {r.stdout}\nstderr: {r.stderr}"
        )
        out = r.stdout.rstrip("\n")
        err = r.stderr.rstrip("\n")
        if out_exact is not None:
            assert out == out_exact, f"stdout {out!r} != {out_exact!r}"
        for pat in out_like:
            assert re.search(pat, out), f"stdout {out!r} did not match {pat!r}"
        for pat in out_unlike:
            assert not re.search(pat, out), f"stdout {out!r} unexpectedly matched {pat!r}"
        if err_exact is not None:
            assert err == err_exact, f"stderr {err!r} != {err_exact!r}"
        for pat in err_like:
            assert re.search(pat, err), f"stderr {err!r} did not match {pat!r}"
        for pat in err_unlike:
            assert not re.search(pat, err), f"stderr {err!r} unexpectedly matched {pat!r}"

    # Create temporary roles and log table.
    psql_command(
        "CREATE ROLE regress_alice WITH LOGIN;"
        " CREATE ROLE regress_mallory WITH LOGIN;"
        " CREATE TABLE user_logins(id serial, who text);"
        " GRANT SELECT ON user_logins TO public;",
        out_exact="",
        err_exact="",
    )

    # Create login event function and trigger.
    psql_command(
        """CREATE FUNCTION on_login_proc() RETURNS event_trigger AS $$
BEGIN
  INSERT INTO user_logins (who) VALUES (SESSION_USER);
  IF SESSION_USER = 'regress_mallory' THEN
    RAISE EXCEPTION 'Hello %! You are NOT welcome here!', SESSION_USER;
  END IF;
  RAISE NOTICE 'Hello %! You are welcome!', SESSION_USER;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;""",
        out_exact="",
        err_exact="",
    )

    psql_command(
        "CREATE EVENT TRIGGER on_login_trigger "
        "ON login EXECUTE PROCEDURE on_login_proc();",
        out_exact="",
        err_exact="",
    )
    psql_command(
        "ALTER EVENT TRIGGER on_login_trigger ENABLE ALWAYS;",
        out_exact="",
        err_like=[r"You are welcome"],
    )

    # Check the two requests were logged via login trigger.
    psql_command(
        "SELECT COUNT(*) FROM user_logins;",
        out_exact="2",
        err_like=[r"You are welcome"],
    )

    # Try to login as allowed Alice. We don't check the Mallory login, because
    # a FATAL error could cause a timing-dependent failure.
    psql_command(
        "SELECT 1;",
        connstr="user=regress_alice",
        out_exact="1",
        err_like=[r"You are welcome"],
        err_unlike=[r"You are NOT welcome"],
    )

    # Check that Alice's login record is here.
    psql_command(
        "SELECT * FROM user_logins;",
        out_like=[r"3\|regress_alice"],
        out_unlike=[r"regress_mallory"],
        err_like=[r"You are welcome"],
    )

    # Check total number of successful logins so far.
    psql_command(
        "SELECT COUNT(*) FROM user_logins;",
        out_exact="5",
        err_like=[r"You are welcome"],
    )

    # Cleanup the temporary stuff.
    psql_command(
        "DROP EVENT TRIGGER on_login_trigger;",
        out_exact="",
        err_like=[r"You are welcome"],
    )
    psql_command(
        "DROP TABLE user_logins;"
        " DROP FUNCTION on_login_proc;"
        " DROP ROLE regress_mallory;"
        " DROP ROLE regress_alice;",
        out_exact="",
        err_exact="",
    )
