# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/authentication/t/002_saslprep.pl.

Tests SCRAM password normalization (SASLprep, RFC 4013). Roles are created
with passwords containing characters that SASLprep maps, decomposes or
prohibits, and we check which client-supplied passwords authenticate. The
cluster forces C locale and UTF-8 so non-ASCII passwords round-trip.

Passwords are passed straight to ``pg.connect(password=...)`` as Python
strings; the Perl byte sequences (e.g. ``"I\\xc2\\xadX"``) are their UTF-8
encodings, so the equivalent Unicode strings (``"I\\u00adX"``) are used here.

This test can only run with Unix-domain sockets (the framework already uses
them on non-Windows platforms).
"""

import sys

import pytest

from libpq import LibpqError

# These tests authenticate over Unix-domain sockets, which the framework only
# uses on non-Windows platforms.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="requires Unix-domain sockets"
)


def check_login(pg, role, password, ok):
    if ok:
        with pg.connect(user=role, password=password):
            pass
    else:
        with pytest.raises(LibpqError):
            with pg.connect(user=role, password=password):
                pass


def test_saslprep(create_pg):
    # Force UTF-8 encoding so we can use non-ASCII characters in the passwords.
    pg = create_pg("primary", initdb_opts=["--locale=C", "--encoding=UTF8"])

    # These tests are based on the example strings from RFC4013, Section 3:
    #
    #   #  Input            Output     Comments
    #   1  I<U+00AD>X       IX         SOFT HYPHEN mapped to nothing
    #   2  user             user       no transformation
    #   3  USER             USER       case preserved, will not match #2
    #   4  <U+00AA>         a          output is NFKC, input in ISO 8859-1
    #   5  <U+2168>         IX         output is NFKC, will match #1
    #   6  <U+0007>                    Error - prohibited character
    #   7  <U+0627><U+0031>            Error - bidirectional check

    # Create test roles.
    pg.sql(
        "SET password_encryption='scram-sha-256';"
        " SET client_encoding='utf8';"
        " CREATE ROLE saslpreptest1_role LOGIN PASSWORD 'IX';"
        " CREATE ROLE saslpreptest4a_role LOGIN PASSWORD 'a';"
        " CREATE ROLE saslpreptest4b_role LOGIN PASSWORD E'\\xc2\\xaa';"
        " CREATE ROLE saslpreptest6_role LOGIN PASSWORD E'foo\\x07bar';"
        " CREATE ROLE saslpreptest7_role LOGIN PASSWORD E'foo\\u0627\\u0031bar';"
    )

    # Require password from now on.
    pg.reset_hba("all", "all", "scram-sha-256")

    # Check that #1 (I + SOFT HYPHEN + X) and #5 (ROMAN NUMERAL NINE) are
    # treated the same as just 'IX'.
    check_login(pg, "saslpreptest1_role", "I­X", True)
    check_login(pg, "saslpreptest1_role", "Ⅸ", True)

    # but different from lower case 'ix'.
    check_login(pg, "saslpreptest1_role", "ix", False)

    # Check #4 (FEMININE ORDINAL INDICATOR normalizes to 'a').
    check_login(pg, "saslpreptest4a_role", "a", True)
    check_login(pg, "saslpreptest4a_role", "ª", True)
    check_login(pg, "saslpreptest4b_role", "a", True)
    check_login(pg, "saslpreptest4b_role", "ª", True)

    # Check #6 and #7 - In PostgreSQL, contrary to the spec, if the password
    # contains prohibited characters, we use it as is, without normalization.
    check_login(pg, "saslpreptest6_role", "foo\x07bar", True)
    check_login(pg, "saslpreptest6_role", "foobar", False)

    check_login(pg, "saslpreptest7_role", "fooا1bar", True)
    check_login(pg, "saslpreptest7_role", "foo1اbar", False)
    check_login(pg, "saslpreptest7_role", "foobar", False)
