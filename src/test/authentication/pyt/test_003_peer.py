# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/authentication/t/003_peer.pl.

Tests peer authentication and user name maps (pg_ident.conf). Each case
rewrites pg_ident.conf with a single mapping (exact, keyword ``all``, regular
expressions, ``\\1`` backreferences, and ``+group`` membership), then connects
as a database role and checks success/failure against the server log.

Skipped unless the platform supports peer authentication, which is detected by
attaching a peer rule and probing the log for the "not supported" message. Can
only run with Unix-domain sockets (the framework already uses them on
non-Windows platforms).
"""

import re

import pytest

from libpq import LibpqError


def test_peer(create_pg):
    pg = create_pg(
        "node",
        conf=[
            "log_connections = authentication",
            # Needed to inspect the postmaster log on failed connections.
            "log_min_messages = debug2",
        ],
    )

    def test_role(role, ok, like=()):
        """Connect as ``role`` over peer auth, asserting success/failure and
        that the server log eventually matches each pattern in ``like``."""
        offset = pg.current_log_position()
        if ok:
            with pg.connect(user=role):
                pass
        else:
            with pytest.raises(LibpqError):
                with pg.connect(user=role):
                    pass
        for pat in like:
            pg.wait_for_log(pat, offset)

    # Set pg_hba.conf with peer authentication.
    pg.reset_hba("all", "all", "peer")

    # Check if peer authentication is supported on this platform.
    offset = pg.current_log_position()
    try:
        with pg.connect():
            pass
    except LibpqError:
        pass
    if re.search(
        r"peer authentication is not supported on this platform",
        pg.log_since(offset),
    ):
        pytest.skip("peer authentication is not supported on this platform")

    # Add a database role and a group, to use for the user name map.
    pg.sql("CREATE ROLE testmapuser LOGIN")
    pg.sql("CREATE ROLE testmapgroup NOLOGIN")
    pg.sql("GRANT testmapgroup TO testmapuser")
    # Note the backslash in the role name here.
    pg.sql(r'CREATE ROLE "testmapgroupliteral\1" LOGIN')
    pg.sql(r'GRANT "testmapgroupliteral\1" TO testmapuser')

    # Extract the system user for the user name map.
    system_user = pg.sql("select (string_to_array(SYSTEM_USER, ':'))[2]")
    su = re.escape(system_user)

    # While on it, check the status of huge pages, that can be either on or
    # off, but never unknown.
    assert pg.sql("SHOW huge_pages_status;") != "unknown", "check huge_pages_status"

    authenticated = rf'connection authenticated: identity="{su}" method=peer'
    no_match = r'no match in usermap "mypeermap" for user "testmapuser"'

    # Tests without the user name map. Failure as connection is attempted with
    # a database role not mapping to an authorized system user.
    test_role(
        "testmapuser",
        False,
        like=[r'Peer authentication failed for user "testmapuser"'],
    )

    # Tests with a user name map.
    pg.reset_ident("mypeermap", system_user, "testmapuser")
    pg.reset_hba("all", "all", "peer map=mypeermap")

    # Success as the database role matches with the system user in the map.
    test_role("testmapuser", True, like=[authenticated])

    # Tests with the "all" keyword.
    pg.reset_ident("mypeermap", system_user, "all")
    test_role("testmapuser", True, like=[authenticated])

    # Tests with the "all" keyword, but quoted (no effect here).
    pg.reset_ident("mypeermap", system_user, '"all"')
    test_role("testmapuser", False, like=[no_match])

    # Success as the regexp of the database user matches.
    pg.reset_ident("mypeermap", system_user, r"/^testm.*$")
    test_role("testmapuser", True, like=[authenticated])

    # Failure as the regexp of the database user does not match.
    pg.reset_ident("mypeermap", system_user, r"/^doesnotmatch.*$")
    test_role("testmapuser", False, like=[no_match])

    # Test with regular expression in user name map. Extract the last 3
    # characters from the system_user, or the entire system_user name (if its
    # length is <= 3). We trust this will not include any regex metacharacters.
    regex_test_string = system_user[-3:]

    # Success as the system user regular expression matches.
    pg.reset_ident("mypeermap", rf"/^.*{regex_test_string}$", "testmapuser")
    test_role("testmapuser", True, like=[authenticated])

    # Success as both regular expressions match.
    pg.reset_ident("mypeermap", rf"/^.*{regex_test_string}$", r"/^testm.*$")
    test_role("testmapuser", True, like=[authenticated])

    # Success as the regular expression matches and database role is the "all"
    # keyword.
    pg.reset_ident("mypeermap", rf"/^.*{regex_test_string}$", "all")
    test_role("testmapuser", True, like=[authenticated])

    # Create target role for \1 tests.
    mapped_name = f"test{regex_test_string}map{regex_test_string}user"
    pg.sql(f"CREATE ROLE {mapped_name} LOGIN")

    # Success as the regular expression matches and \1 is replaced in the given
    # subexpression.
    pg.reset_ident("mypeermap", rf"/^.*({regex_test_string})$", r"test\1map\1user")
    test_role(mapped_name, True, like=[authenticated])

    # Success as the regular expression matches and \1 is replaced in the given
    # subexpression, even if quoted.
    pg.reset_ident("mypeermap", rf"/^.*({regex_test_string})$", r'"test\1map\1user"')
    test_role(mapped_name, True, like=[authenticated])

    # Failure as the regular expression does not include a subexpression, but
    # the database user contains \1, requesting a replacement.
    pg.reset_ident("mypeermap", rf"/^{system_user}$", r"\1testmapuser")
    test_role(
        "testmapuser",
        False,
        like=[
            rf'regular expression "\^{su}\$" has no subexpressions as requested '
            r'by backreference in "\\1testmapuser"'
        ],
    )

    # Concatenate system_user to system_user.
    bad_regex_test_string = system_user + system_user

    # Failure as the regexp of system user does not match.
    pg.reset_ident("mypeermap", rf"/^.*{bad_regex_test_string}$", "testmapuser")
    test_role("testmapuser", False, like=[no_match])

    # Test using a group role match for the database user.
    pg.reset_ident("mypeermap", system_user, "+testmapgroup")
    test_role("testmapuser", True, like=[authenticated])
    test_role(
        "testmapgroup",
        False,
        like=[r'role "testmapgroup" is not permitted to log in'],
    )

    # Now apply quotes to the group match, nullifying its effect.
    pg.reset_ident("mypeermap", system_user, '"+testmapgroup"')
    test_role("testmapuser", False, like=[no_match])

    # Test using a regexp for the system user, with a group membership check
    # for the database user.
    pg.reset_ident("mypeermap", rf"/^.*{regex_test_string}$", "+testmapgroup")
    test_role("testmapuser", True, like=[authenticated])
    test_role(
        "testmapgroup",
        False,
        like=[r'role "testmapgroup" is not permitted to log in'],
    )

    # Test that membership checks and regexes will use literal \1 instead of
    # replacing it, as subexpression replacement is not allowed in this case.
    pg.reset_ident(
        "mypeermap", rf"/^.*{regex_test_string}(.*)$", r"+testmapgroupliteral\1"
    )
    test_role("testmapuser", True, like=[authenticated])

    # Do the same with a quoted regular expression for the database user this
    # time. No replacement of \1 is done.
    pg.reset_ident(
        "mypeermap", rf"/^.*{regex_test_string}(.*)$", r'"/^testmapgroupliteral\\1$"'
    )
    test_role("testmapgroupliteral\\1", True, like=[authenticated])
