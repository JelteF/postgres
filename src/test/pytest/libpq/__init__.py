# Copyright (c) 2025, PostgreSQL Global Development Group

"""
libpq testing utilities - ctypes bindings and helpers for PostgreSQL's libpq library.

This module provides Python wrappers around libpq for use in pytest tests.
"""

from __future__ import annotations

from . import errors
from .errors import (
    LibpqError,
    PostgresMessage,
    PostgresNotice,
    PostgresWarning,
)
from ._bindings import (
    ConnectionStatus,
    DiagField,
    ExecStatus,
    load_libpq_handle,
)
from ._conversions import register_type_info
from ._core import (
    Notify,
    PGconn,
    PGresult,
    PreparedStatement,
    connect,
    connstr,
)

__all__ = [
    "errors",
    "LibpqError",
    "ConnectionStatus",
    "DiagField",
    "ExecStatus",
    "Notify",
    "PGconn",
    "PostgresMessage",
    "PostgresNotice",
    "PostgresWarning",
    "PGresult",
    "PreparedStatement",
    "connect",
    "connstr",
    "load_libpq_handle",
    "register_type_info",
]
