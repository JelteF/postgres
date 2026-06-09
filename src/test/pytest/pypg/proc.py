# Copyright (c) 2025, PostgreSQL Global Development Group

"""Helpers for invoking installed PostgreSQL programs in tests.

This is the pytest replacement for the Perl ``PostgreSQL::Test::Utils``
``command_*`` and ``program_*_ok`` helpers. A :class:`PgBin` resolves program
names against the test bindir and runs them with output captured, returning a
:class:`subprocess.CompletedProcess` so tests can assert with plain pytest
idioms::

    def test_version(pg_bin):
        assert "PostgreSQL" in pg_bin.run("pg_config", "--version").stdout

The ``check_*`` methods bundle the behavioural conventions that every client
program must satisfy (a working ``--help``/``--version`` and rejection of
unknown options), since nearly every ``src/bin`` suite exercises them.
"""

import os
import pathlib
import re
import subprocess

from .util import run

# The --help convention enforces a maximum line length. This value isn't set in
# stone; it reflects the current project convention (most output aims for 80).
_MAX_HELP_LINE = 95


class PgBin:
    """Runs installed PostgreSQL programs from the test bindir."""

    def __init__(self, bindir):
        self.bindir = pathlib.Path(bindir)

    def run(self, name, *args, check=False, server=None, addenv=None, **kwargs):
        """Run program ``name`` from the bindir with stdout/stderr captured.

        Unlike :func:`pypg.util.run` this defaults to ``check=False``: most
        program tests want to inspect a nonzero exit code rather than raise on
        it. Pass ``check=True`` when a failure should abort the test.

        Pass ``server=`` a :class:`PostgresServer` to run the program against
        it: the server's PG* connection variables (host, port, database) are
        merged into the environment so the program connects without explicit
        connection arguments, mirroring the Perl ``$node->command_*`` helpers.

        ``addenv`` is a dict of extra environment variables to set (e.g.
        ``PGOPTIONS``), layered on top of the server's connection environment.
        """
        if server is not None or addenv is not None:
            env = kwargs.pop("env", None)
            env = dict(env if env is not None else os.environ)
            if server is not None:
                env.update(server.connection_env())
            if addenv is not None:
                env.update(addenv)
            kwargs["env"] = env
        return run(
            self.bindir / name,
            *args,
            check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            **kwargs,
        )

    def check_all(self, name, *args, exit_code=0, stdout=(), stderr=(), server=None, **kwargs):
        """Run a program and assert its exit code and output, like the Perl
        ``command_checks_all``.

        ``stdout``/``stderr`` are iterables of regex patterns that must each be
        found (``re.search``, DOTALL) in the respective stream. Returns the
        completed process so callers can make further assertions.
        """
        r = self.run(name, *args, server=server, **kwargs)
        assert r.returncode == exit_code, (
            f"expected exit {exit_code}, got {r.returncode}\n"
            f"stdout: {r.stdout}\nstderr: {r.stderr}"
        )
        for pattern in stdout:
            assert re.search(pattern, r.stdout, re.S), (
                f"stdout did not match {pattern!r}\nstdout: {r.stdout}"
            )
        for pattern in stderr:
            assert re.search(pattern, r.stderr, re.S), (
                f"stderr did not match {pattern!r}\nstderr: {r.stderr}"
            )
        return r

    def check_help(self, name):
        """``--help`` exits 0, writes only to stdout, and respects the line limit."""
        r = self.run(name, "--help")
        assert r.returncode == 0, r.stderr
        assert r.stdout != "", "--help wrote nothing to stdout"
        assert r.stderr == "", f"--help wrote to stderr: {r.stderr}"
        too_long = [ln for ln in r.stdout.splitlines() if len(ln) > _MAX_HELP_LINE]
        assert not too_long, f"--help lines exceed {_MAX_HELP_LINE} chars: {too_long}"

    def check_version(self, name):
        """``--version`` exits 0 and writes only to stdout."""
        r = self.run(name, "--version")
        assert r.returncode == 0, r.stderr
        assert r.stdout != "", "--version wrote nothing to stdout"
        assert r.stderr == "", f"--version wrote to stderr: {r.stderr}"

    def check_bad_option(self, name):
        """An unknown option exits nonzero and writes an error to stderr."""
        r = self.run(name, "--not-a-valid-option")
        assert r.returncode != 0, "expected nonzero exit for an invalid option"
        assert r.stderr != "", "expected an error message on stderr"
