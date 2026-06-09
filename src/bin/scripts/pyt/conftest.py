# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Shared helpers for the src/bin/scripts pytest suite.

The Perl tests use one fresh node per file and lean heavily on
``$node->issues_sql_like``: run a client program against the server and check
the SQL it sent, captured from the server log. The Perl test framework enables
``log_statement = all`` on every node; the ``node`` fixture here does the same
so those statements show up, and ``sql_like``/``sql_unlike`` wrap the
run-and-grep dance.

A dedicated server per test (rather than the shared session server) matches the
Perl model and keeps the databases/roles these tests create from leaking
between tests.
"""

import re

import pytest


@pytest.fixture
def node(request, create_pg):
    """A fresh server for one test, logging every statement."""
    name = re.sub(r"[^A-Za-z0-9_]", "_", request.node.nodeid)
    s = create_pg(name)
    s.append_conf("log_statement = all")
    s.pg_ctl("reload")
    return s


@pytest.fixture
def sql_like(pg_bin):
    """Run a client program against ``node``, assert it exits 0, and assert
    that the SQL it issued (captured from the server log) matches the given
    regex. The pytest replacement for ``$node->issues_sql_like``."""

    def _check(node, args, pattern, flags=re.S):
        offset = node.current_log_position()
        r = pg_bin.run(*args, server=node)
        assert r.returncode == 0, r.stderr
        log = node.log_since(offset)
        assert re.search(pattern, log, flags), (
            f"pattern {pattern!r} not found in server log:\n{log}"
        )
        return r

    return _check


@pytest.fixture
def sql_unlike(pg_bin):
    """Like ``sql_like`` but asserts the SQL the program issued does *not*
    match the pattern (``$node->issues_sql_unlike``)."""

    def _check(node, args, pattern, flags=re.S):
        offset = node.current_log_position()
        r = pg_bin.run(*args, server=node)
        assert r.returncode == 0, r.stderr
        log = node.log_since(offset)
        assert not re.search(pattern, log, flags), (
            f"pattern {pattern!r} unexpectedly found in server log:\n{log}"
        )
        return r

    return _check
