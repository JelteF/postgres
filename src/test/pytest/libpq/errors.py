# Copyright (c) 2025, PostgreSQL Global Development Group

"""
Exception and warning classes for libpq.

Errors the server reports on a failed result are raised as ``LibpqError``;
NOTICE/WARNING/... messages it reports on a successful result are surfaced as
the ``PostgresMessage`` warning categories. Both sides share
``PostgresDiagnostics`` so a caught error or warning exposes the same
``.detail``/``.hint``/``.sqlstate`` fields.
"""

from __future__ import annotations


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

    sqlstate: str | None
    severity: str | None
    primary: str | None
    detail: str | None
    hint: str | None
    schema_name: str | None
    table_name: str | None
    column_name: str | None
    datatype_name: str | None
    constraint_name: str | None
    position: int | None
    context: str | None

    def __init__(
        self,
        message: str,
        *,
        sqlstate: str | None = None,
        severity: str | None = None,
        primary: str | None = None,
        detail: str | None = None,
        hint: str | None = None,
        schema_name: str | None = None,
        table_name: str | None = None,
        column_name: str | None = None,
        datatype_name: str | None = None,
        constraint_name: str | None = None,
        position: int | None = None,
        context: str | None = None,
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
    def sqlstate_class(self) -> str | None:
        """Returns the 2-character SQLSTATE class."""
        if self.sqlstate and len(self.sqlstate) >= 2:
            return self.sqlstate[:2]
        return None


class LibpqError(PostgresDiagnostics, RuntimeError):
    """Exception for libpq errors with PostgreSQL diagnostic fields."""


class PostgresMessage(PostgresDiagnostics, UserWarning):
    """Base category for server messages surfaced over libpq as Python warnings.

    A message the server sends outside of an error result (a NOTICE, WARNING,
    INFO, ... — what psql prints to stderr) is reported as a Python warning, so
    tests can assert on it with ``pytest.warns(..., match=...)``. WARNING and
    NOTICE map to the subclasses below; any other level (INFO, LOG, DEBUG, ...)
    is reported as this base category directly.

    Like ``LibpqError`` it carries the full set of diagnostic fields the server
    attached to the result (see ``PostgresDiagnostics``), so a caught warning
    exposes ``.detail``, ``.hint``, ``.sqlstate``, etc.
    """


class PostgresNotice(PostgresMessage):
    """A NOTICE message reported by the server over libpq."""


class PostgresWarning(PostgresMessage):
    """A WARNING message reported by the server over libpq."""
