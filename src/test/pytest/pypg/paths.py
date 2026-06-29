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

import functools
import os
import pathlib

from .util import capture


@functools.cache
def pg_config() -> str:
    """Path to pg_config: the ``PG_CONFIG`` environment variable if set,
    otherwise ``pg_config`` from ``PATH``."""
    return os.environ.get("PG_CONFIG", "pg_config")


@functools.cache
def _config_path(flag: str) -> pathlib.Path:
    return pathlib.Path(capture(pg_config(), flag, silent=True))


def bindir() -> pathlib.Path:
    """PostgreSQL bin directory (``pg_config --bindir``)."""
    return _config_path("--bindir")


def libdir() -> pathlib.Path:
    """PostgreSQL lib directory (``pg_config --libdir``)."""
    return _config_path("--libdir")


def sharedir() -> pathlib.Path:
    """PostgreSQL share directory (``pg_config --sharedir``)."""
    return _config_path("--sharedir")


def includedir_server() -> pathlib.Path:
    """PostgreSQL server include directory (``pg_config --includedir-server``)."""
    return _config_path("--includedir-server")
