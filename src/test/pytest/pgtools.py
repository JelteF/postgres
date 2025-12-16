# Copyright (c) 2025, PostgreSQL Global Development Group

"""
Install-location discovery for the PostgreSQL build under test.

These are plain cached functions -- not pytest fixtures and not import-time
globals. The values are static for the whole session, so routing them through
fixture signatures was needless plumbing; and computing them at import time
would run pg_config (a subprocess) on every ``import``, crashing all collection
if pg_config isn't found. As lazy functions they run pg_config only when
something actually needs a path.

This is a standalone module (depending only on the standard library) so that
both ``libpq`` and ``pypg`` can locate the install without importing each
other -- importing one from the other forms a cycle.
"""

import functools
import os
import pathlib
import subprocess


@functools.cache
def pg_config() -> str:
    """Path to pg_config: the ``PG_CONFIG`` environment variable if set,
    otherwise ``pg_config`` from ``PATH``."""
    return os.environ.get("PG_CONFIG", "pg_config")


@functools.cache
def _config_path(flag: str) -> pathlib.Path:
    out = subprocess.run(
        [pg_config(), flag],
        check=True,
        stdout=subprocess.PIPE,
        encoding="utf-8",
    ).stdout.strip()
    return pathlib.Path(out)


def bindir() -> pathlib.Path:
    """PostgreSQL bin directory (``pg_config --bindir``)."""
    return _config_path("--bindir")


def libdir() -> pathlib.Path:
    """PostgreSQL lib directory (``pg_config --libdir``)."""
    return _config_path("--libdir")


def sharedir() -> pathlib.Path:
    """PostgreSQL share directory (``pg_config --sharedir``)."""
    return _config_path("--sharedir")
