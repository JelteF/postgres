# Copyright (c) 2025, PostgreSQL Global Development Group

"""
PostgreSQL error types mapped from SQLSTATE codes.

This module provides LibpqError and its subclasses for handling PostgreSQL
errors based on SQLSTATE codes. The exception classes in _generated_errors.py
are auto-generated from src/backend/utils/errcodes.txt.

To regenerate: src/tools/generate_pytest_libpq_errors.py
"""

from typing import Optional

from ._error_base import LibpqError, LibpqWarning
from ._generated_errors import (
    SQLSTATE_TO_EXCEPTION,
)
from ._generated_errors import *  # noqa: F403


def get_exception_class(sqlstate: Optional[str]) -> type:
    """Get the appropriate exception class for a SQLSTATE code."""
    if sqlstate in SQLSTATE_TO_EXCEPTION:
        return SQLSTATE_TO_EXCEPTION[sqlstate]
    return LibpqError


def make_error(message: str, *, sqlstate: Optional[str] = None, **kwargs) -> LibpqError:
    """Create an appropriate LibpqError subclass based on the SQLSTATE code."""
    exc_class = get_exception_class(sqlstate)
    return exc_class(message, sqlstate=sqlstate, **kwargs)


__all__ = [
    "LibpqError",
    "LibpqWarning",
    "make_error",
]
