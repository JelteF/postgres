# Copyright (c) 2025, PostgreSQL Global Development Group

"""
Exception classes for libpq errors.
"""

from typing import Optional


class PostgresDiagnostics(Exception):
    """Holds the PostgreSQL diagnostic fields (SQLSTATE, detail, hint, ...) the
    server attaches to a result.

    The server sends the same set of fields on an error result as on a
    NOTICE/WARNING result, so this is mixed into both ``LibpqError`` (raised for
    error results) and ``PostgresMessage`` (warned for NOTICE/WARNING results).
    That way a caught notice exposes ``.detail``, ``.constraint_name``, etc.
    exactly like a caught error does, rather than only its message text.

    It roots at ``Exception`` — which both concrete bases (``RuntimeError`` and
    ``UserWarning``) already derive from — so that ``super().__init__(message)``
    cooperatively reaches the real base and stores the message as the
    exception/warning argument as usual.
    """

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


class LibpqError(PostgresDiagnostics, RuntimeError):
    """Exception for libpq errors with PostgreSQL diagnostic fields."""
