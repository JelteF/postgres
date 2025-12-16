# Copyright (c) 2025, PostgreSQL Global Development Group

"""
Base exception classes for libpq errors and warnings.
"""

from typing import Optional


class LibpqExceptionMixin:
    """Mixin providing PostgreSQL error field attributes."""

    sqlstate: Optional[str]
    severity: Optional[str]
    primary: Optional[str]
    detail: Optional[str]
    hint: Optional[str]
    schema_name: Optional[str]
    table_name: Optional[str]
    column_name: Optional[str]
    datatype_name: Optional[str]
    constraint_name: Optional[str]
    position: Optional[int]
    context: Optional[str]

    def __init__(
        self,
        message: str,
        *,
        sqlstate: Optional[str] = None,
        severity: Optional[str] = None,
        primary: Optional[str] = None,
        detail: Optional[str] = None,
        hint: Optional[str] = None,
        schema_name: Optional[str] = None,
        table_name: Optional[str] = None,
        column_name: Optional[str] = None,
        datatype_name: Optional[str] = None,
        constraint_name: Optional[str] = None,
        position: Optional[int] = None,
        context: Optional[str] = None,
    ):
        super().__init__(message)
        self.sqlstate = sqlstate
        self.severity = severity
        self.primary = primary
        self.detail = detail
        self.hint = hint
        self.schema_name = schema_name
        self.table_name = table_name
        self.column_name = column_name
        self.datatype_name = datatype_name
        self.constraint_name = constraint_name
        self.position = position
        self.context = context

    @property
    def sqlstate_class(self) -> Optional[str]:
        """Returns the 2-character SQLSTATE class."""
        if self.sqlstate and len(self.sqlstate) >= 2:
            return self.sqlstate[:2]
        return None


class LibpqError(LibpqExceptionMixin, RuntimeError):
    """Base exception for libpq errors."""

    pass


class LibpqWarning(LibpqExceptionMixin, UserWarning):
    """Base exception for libpq warnings."""

    pass
