# Copyright (c) 2025, PostgreSQL Global Development Group

"""
Install-location discovery for the PostgreSQL build under test.

These are plain cached functions -- not pytest fixtures and not import-time
globals. The values are static for the whole session, so routing them through
fixture signatures would be needless plumbing; and computing them at import
time would run pg_config (a subprocess) on every ``import``, breaking all
collection if pg_config isn't found. As lazy cached functions they run
pg_config only when something actually needs a path, and only once.
"""

from __future__ import annotations

import functools
import os
import pathlib

from .util import capture


@functools.cache
def _config_values() -> dict[str, str]:
    """All pg_config settings, from a single argument-less pg_config run, which
    prints every setting as a ``NAME = value`` line. pg_config is found via the
    ``PG_CONFIG`` environment variable, falling back to ``PATH``."""
    pg_config = os.environ.get("PG_CONFIG", "pg_config")
    values = {}
    for line in capture(pg_config, silent=True).splitlines():
        name, sep, value = line.partition(" = ")
        if sep:
            values[name] = value
    return values


def _config_path(name: str) -> pathlib.Path:
    return pathlib.Path(_config_values()[name])


@functools.cache
def bindir() -> pathlib.Path:
    """PostgreSQL bin directory (pg_config's ``BINDIR``)."""
    return _config_path("BINDIR")


@functools.cache
def libdir() -> pathlib.Path:
    """PostgreSQL lib directory (pg_config's ``LIBDIR``)."""
    return _config_path("LIBDIR")


@functools.cache
def sharedir() -> pathlib.Path:
    """PostgreSQL share directory (pg_config's ``SHAREDIR``)."""
    return _config_path("SHAREDIR")


@functools.cache
def includedir_server() -> pathlib.Path:
    """PostgreSQL server include directory (pg_config's ``INCLUDEDIR-SERVER``)."""
    return _config_path("INCLUDEDIR-SERVER")
