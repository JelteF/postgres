# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/scripts/t/200_connstr.pl.

Checks that the --all options of vacuumdb/reindexdb/clusterdb cope with
databases whose names span the full range of LATIN1 byte values (including a
'=' and other bytes that are not valid UTF-8). The utilities enumerate the
databases from the catalog and reconnect to each by name, so the names have to
round-trip through the client correctly.
"""

import os
import subprocess

import pytest

from pypg.bins import vacuumdb, reindexdb, clusterdb
from pypg.paths import BINDIR

# These byte sequences aren't valid UTF-8. Mirror the Perl test's environment:
# LATIN1 accepts any byte and maps each one to a UTF-8 character, so the client
# can interpret the catalog database names, and LC_ALL=C avoids any locale
# interfering with the byte handling.
_ENV = {"LC_ALL": "C", "PGCLIENTENCODING": "LATIN1"}


def _ascii_bytes(from_char, to_char):
    """The pytest equivalent of Perl's generate_ascii_string(from, to): the
    bytes from ``from_char`` through ``to_char`` inclusive. Returned as bytes
    rather than str because the higher ranges are not valid UTF-8 and the names
    are passed verbatim on argv to createdb."""
    return bytes(range(from_char, to_char + 1))


# Database names covering the LATIN1 range. dbname1 contains '=' (byte 61),
# which a naive conninfo builder would mistake for a keyword separator. 64-66
# are skipped in dbname2 so its length stays at the 63-byte identifier limit
# (NAMEDATALEN-1); the others stay within that bound by construction.
_DBNAME1 = _ascii_bytes(1, 63)
_DBNAME2 = _ascii_bytes(67, 129)
_DBNAME3 = _ascii_bytes(130, 192)
_DBNAME4 = _ascii_bytes(193, 255)


@pytest.fixture(scope="module")
def node(create_pg_module):
    """A dedicated cluster initialized with --locale=C --encoding=LATIN1.

    The shared module server uses the default locale/encoding and cannot hold
    LATIN1-only database names, so this test needs its own cluster (initdb
    options force a real initdb rather than the template copy)."""
    server = create_pg_module(
        "connstr", initdb_opts=["--locale=C", "--encoding=LATIN1"]
    )

    # createdb's name argument carries raw (non-UTF-8) bytes, so the program is
    # invoked through subprocess directly: PgBin coerces every argv entry with
    # str(), which would mangle a bytes name. The PG* connection variables and
    # the LATIN1 client environment are layered on by hand.
    env = dict(os.environ)
    env.update(server.connection_env())
    env.update(_ENV)
    for dbname in (_DBNAME1, _DBNAME2, _DBNAME3, _DBNAME4, b"CamelCase"):
        # Like Perl's run_log, the result is intentionally ignored: some names
        # contain bytes createdb rejects (e.g. dbname1 includes a newline and a
        # carriage return), and the point of the test is only that the --all
        # utilities cope with whichever databases do get created.
        try:
            subprocess.run([str(BINDIR / "createdb"), dbname], env=env, check=False)
        except UnicodeError:
            # A Windows command line is Unicode, so Python decodes each argv
            # element as UTF-8 before spawning and a name with non-UTF-8 bytes
            # (dbname2-4) cannot be passed at all. Skip it there, exactly as
            # Perl's createdb fails to create these names on Windows; dbname1
            # (ASCII) and CamelCase still get created, so the --all utilities
            # are still exercised over an unusual name (dbname1 contains '=').
            pass
    return server


def test_vacuumdb_all(node):
    vacuumdb("--all", "--echo", "--analyze-only", server=node, addenv=_ENV)


def test_reindexdb_all(node):
    reindexdb("--all", "--echo", server=node, addenv=_ENV)


def test_clusterdb_all(node):
    clusterdb("--all", "--echo", "--verbose", server=node, addenv=_ENV)
