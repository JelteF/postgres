# Copyright (c) 2025, PostgreSQL Global Development Group

"""
libpq testing utilities - ctypes bindings and helpers for PostgreSQL's libpq library.

This module provides Python wrappers around libpq for use in pytest tests.
"""

from . import errors
from .errors import LibpqError
from ._core import (
    ConnectionStatus,
    DiagField,
    ExecStatus,
    Notify,
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
    "ConnectionStatus",
    "DiagField",
    "ExecStatus",
    "Notify",
    "PGconn",
    "PGresult",
    "connect",
    "connstr",
    "load_libpq_handle",
    "register_type_info",
]
