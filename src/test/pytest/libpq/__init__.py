# Copyright (c) 2025, PostgreSQL Global Development Group

"""
libpq testing utilities - ctypes bindings and helpers for PostgreSQL's libpq library.

This module provides Python wrappers around libpq for use in pytest tests.
"""

from . import errors
from .errors import LibpqError, LibpqWarning
from ._core import (
    ConnectionStatus,
    DiagField,
    ExecStatus,
    PGconn,
    PGresult,
    connect,
    connstr,
    load_libpq_handle,
    register_type_info,
)

__all__ = [
    "errors",
    "LibpqError",
    "LibpqWarning",
    "ConnectionStatus",
    "DiagField",
    "ExecStatus",
    "PGconn",
    "PGresult",
    "connect",
    "connstr",
    "load_libpq_handle",
    "register_type_info",
]
