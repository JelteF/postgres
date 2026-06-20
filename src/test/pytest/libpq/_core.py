# Copyright (c) 2025, PostgreSQL Global Development Group

"""
Core libpq functionality - ctypes bindings and connection handling.
"""

import contextlib
import ctypes
import datetime
import decimal
import enum
import json
import platform
import os
import uuid
import warnings
from collections import namedtuple
from typing import Any, Callable, Dict, Optional

from .errors import LibpqError


class PostgresMessage(UserWarning):
    """Base category for server messages surfaced over libpq as Python warnings.

    A message the server sends outside of an error result (a NOTICE, WARNING,
    INFO, ... — what psql prints to stderr) is reported as a Python warning, so
    tests can assert on it with ``pytest.warns(..., match=...)``. WARNING and
    NOTICE map to the subclasses below; any other level (INFO, LOG, DEBUG, ...)
    is reported as this base category directly.
    """


class PostgresNotice(PostgresMessage):
    """A NOTICE message reported by the server over libpq."""


class PostgresWarning(PostgresMessage):
    """A WARNING message reported by the server over libpq."""


# A LISTEN/NOTIFY notification, as returned by PGconn.notifies().
Notify = namedtuple("Notify", ["channel", "pid", "payload"])


# PG_DIAG field identifiers from postgres_ext.h
class DiagField(enum.IntEnum):
    SEVERITY = ord("S")
    SEVERITY_NONLOCALIZED = ord("V")
    SQLSTATE = ord("C")
    MESSAGE_PRIMARY = ord("M")
    MESSAGE_DETAIL = ord("D")
    MESSAGE_HINT = ord("H")
    STATEMENT_POSITION = ord("P")
    INTERNAL_POSITION = ord("p")
    INTERNAL_QUERY = ord("q")
    CONTEXT = ord("W")
    SCHEMA_NAME = ord("s")
    TABLE_NAME = ord("t")
    COLUMN_NAME = ord("c")
    DATATYPE_NAME = ord("d")
    CONSTRAINT_NAME = ord("n")
    SOURCE_FILE = ord("F")
    SOURCE_LINE = ord("L")
    SOURCE_FUNCTION = ord("R")


class ConnectionStatus(enum.IntEnum):
    """PostgreSQL connection status codes from libpq."""

    CONNECTION_OK = 0
    CONNECTION_BAD = 1


class ExecStatus(enum.IntEnum):
    """PostgreSQL result status codes from PQresultStatus."""

    PGRES_EMPTY_QUERY = 0
    PGRES_COMMAND_OK = 1
    PGRES_TUPLES_OK = 2
    PGRES_COPY_OUT = 3
    PGRES_COPY_IN = 4
    PGRES_BAD_RESPONSE = 5
    PGRES_NONFATAL_ERROR = 6
    PGRES_FATAL_ERROR = 7
    PGRES_COPY_BOTH = 8
    PGRES_SINGLE_TUPLE = 9
    PGRES_PIPELINE_SYNC = 10
    PGRES_PIPELINE_ABORTED = 11


class _PGconn(ctypes.Structure):
    pass


class _PGresult(ctypes.Structure):
    pass


class _PGnotify(ctypes.Structure):
    """Mirror of libpq's PGnotify (postgres_ext.h). Only the public fields are
    used; ``next`` is libpq-internal and kept opaque."""

    _fields_ = [
        ("relname", ctypes.c_char_p),
        ("be_pid", ctypes.c_int),
        ("extra", ctypes.c_char_p),
        ("next", ctypes.c_void_p),
    ]


_PGconn_p = ctypes.POINTER(_PGconn)
_PGresult_p = ctypes.POINTER(_PGresult)
_PGnotify_p = ctypes.POINTER(_PGnotify)

# Signature of a libpq notice receiver: void (*)(void *arg, const PGresult *res).
_NOTICE_RECEIVER = ctypes.CFUNCTYPE(None, ctypes.c_void_p, _PGresult_p)


def load_libpq_handle(libdir, bindir):
    """
    Loads a ctypes handle for libpq. Some common function prototypes are
    initialized for general use.
    """
    system = platform.system()

    if system in ("Linux", "FreeBSD", "NetBSD", "OpenBSD"):
        name = "libpq.so.5"
    elif system == "Darwin":
        name = "libpq.5.dylib"
    elif system == "Windows":
        name = "libpq.dll"
    else:
        assert False, f"the libpq fixture must be updated for {system}"

    if system == "Windows":
        # On Windows, libpq.dll is confusingly in bindir, not libdir.
        #
        # libpq.dll pulls in dependent DLLs (OpenSSL, zstd, ...) that live in
        # bindir or other directories on PATH. ctypes' default load uses
        # LOAD_LIBRARY_SEARCH_DEFAULT_DIRS, which does not search PATH, so those
        # dependencies are not found. winmode=0 selects the standard,
        # PATH-inclusive DLL search instead -- the same way the client
        # executables resolve these DLLs (the test environment puts the
        # install's bin directory on PATH).
        libpq_path = os.path.join(bindir, name)
        lib = ctypes.CDLL(libpq_path, winmode=0)
    else:
        libpq_path = os.path.join(libdir, name)
        lib = ctypes.CDLL(libpq_path)

    #
    # Function Prototypes
    #

    lib.PQconnectdb.restype = _PGconn_p
    lib.PQconnectdb.argtypes = [ctypes.c_char_p]

    lib.PQstatus.restype = ctypes.c_int
    lib.PQstatus.argtypes = [_PGconn_p]

    lib.PQexec.restype = _PGresult_p
    lib.PQexec.argtypes = [_PGconn_p, ctypes.c_char_p]

    lib.PQresultStatus.restype = ctypes.c_int
    lib.PQresultStatus.argtypes = [_PGresult_p]

    lib.PQclear.restype = None
    lib.PQclear.argtypes = [_PGresult_p]

    lib.PQerrorMessage.restype = ctypes.c_char_p
    lib.PQerrorMessage.argtypes = [_PGconn_p]

    lib.PQfinish.restype = None
    lib.PQfinish.argtypes = [_PGconn_p]

    lib.PQresultErrorMessage.restype = ctypes.c_char_p
    lib.PQresultErrorMessage.argtypes = [_PGresult_p]

    lib.PQntuples.restype = ctypes.c_int
    lib.PQntuples.argtypes = [_PGresult_p]

    lib.PQnfields.restype = ctypes.c_int
    lib.PQnfields.argtypes = [_PGresult_p]

    lib.PQgetvalue.restype = ctypes.c_char_p
    lib.PQgetvalue.argtypes = [_PGresult_p, ctypes.c_int, ctypes.c_int]

    lib.PQgetisnull.restype = ctypes.c_int
    lib.PQgetisnull.argtypes = [_PGresult_p, ctypes.c_int, ctypes.c_int]

    lib.PQftype.restype = ctypes.c_uint
    lib.PQftype.argtypes = [_PGresult_p, ctypes.c_int]

    lib.PQresultErrorField.restype = ctypes.c_char_p
    lib.PQresultErrorField.argtypes = [_PGresult_p, ctypes.c_int]

    #
    # Extended-protocol entry points. These are the libpq functions that psql's
    # \bind / \parse / \bind_named meta-commands are built on, so tests can drive
    # the extended protocol directly instead of shelling out to psql.
    #
    _char_pp = ctypes.POINTER(ctypes.c_char_p)

    # PQexecParams: unnamed Parse/Bind/Execute (psql `<query> \bind <params> \g`).
    lib.PQexecParams.restype = _PGresult_p
    lib.PQexecParams.argtypes = [
        _PGconn_p,
        ctypes.c_char_p,  # command
        ctypes.c_int,  # nParams
        ctypes.c_void_p,  # paramTypes
        _char_pp,  # paramValues
        ctypes.c_void_p,  # paramLengths
        ctypes.c_void_p,  # paramFormats
        ctypes.c_int,  # resultFormat
    ]

    # PQprepare: Parse a named statement (psql `<query> \parse <name>`).
    lib.PQprepare.restype = _PGresult_p
    lib.PQprepare.argtypes = [
        _PGconn_p,
        ctypes.c_char_p,  # stmtName
        ctypes.c_char_p,  # query
        ctypes.c_int,  # nParams
        ctypes.c_void_p,  # paramTypes
    ]

    # PQexecPrepared: Bind/Execute a named statement (psql `\bind_named <name> ...`).
    lib.PQexecPrepared.restype = _PGresult_p
    lib.PQexecPrepared.argtypes = [
        _PGconn_p,
        ctypes.c_char_p,  # stmtName
        ctypes.c_int,  # nParams
        _char_pp,  # paramValues
        ctypes.c_void_p,  # paramLengths
        ctypes.c_void_p,  # paramFormats
        ctypes.c_int,  # resultFormat
    ]

    lib.PQgetResult.restype = _PGresult_p
    lib.PQgetResult.argtypes = [_PGconn_p]

    # COPY ... TO STDOUT support: PQexec returns a PGRES_COPY_OUT result and the
    # rows (and any error raised while reading them) are then streamed out.
    lib.PQgetCopyData.restype = ctypes.c_int
    lib.PQgetCopyData.argtypes = [
        _PGconn_p,
        ctypes.POINTER(ctypes.c_char_p),
        ctypes.c_int,
    ]

    lib.PQfreemem.restype = None
    lib.PQfreemem.argtypes = [ctypes.c_void_p]

    # LISTEN/NOTIFY: PQconsumeInput reads any data waiting on the socket into
    # libpq's buffers; PQnotifies then pops queued notifications one at a time.
    lib.PQconsumeInput.restype = ctypes.c_int
    lib.PQconsumeInput.argtypes = [_PGconn_p]

    lib.PQnotifies.restype = _PGnotify_p
    lib.PQnotifies.argtypes = [_PGconn_p]

    # Notice/warning capture. The default behaviour prints to stderr; we install
    # a receiver (which, unlike a processor, gets the full PGresult so we can
    # read the non-localized severity) and turn each message into a Python
    # warning, so tests can assert on what psql shows on stderr.
    lib.PQsetNoticeReceiver.restype = ctypes.c_void_p
    lib.PQsetNoticeReceiver.argtypes = [_PGconn_p, _NOTICE_RECEIVER, ctypes.c_void_p]

    return lib


def _build_params(params):
    """Build the (nParams, paramValues) pair libpq's extended-protocol
    functions expect from a tuple of Python parameter values. Values are
    passed in text format; ``None`` becomes a SQL NULL."""
    if not params:
        return 0, None
    arr = (ctypes.c_char_p * len(params))()
    for i, p in enumerate(params):
        arr[i] = None if p is None else str(p).encode()
    return len(params), arr


# PostgreSQL type OIDs and conversion system
# Type registry - maps OID to converter function
_type_converters: Dict[int, Callable[[str], Any]] = {}
_array_to_elem_map: Dict[int, int] = {}


def register_type_info(
    name: str, oid: int, array_oid: int, converter: Callable[[str], Any]
):
    """
    Register a PostgreSQL type with its OID, array OID, and conversion function.

    Usage:
        register_type_info("bool", 16, 1000, lambda v: v == "t")
    """
    _type_converters[oid] = converter
    if array_oid is not None:
        _array_to_elem_map[array_oid] = oid


def _parse_array(value: str, elem_oid: int):
    """Parse PostgreSQL array syntax into nested Python lists."""
    stack: list[list] = []
    current_element: list[str] = []
    in_quotes = False
    was_quoted = False
    pos = 0

    while pos < len(value):
        char = value[pos]

        if in_quotes:
            if char == "\\":
                next_char = value[pos + 1]
                if next_char not in '"\\':
                    raise NotImplementedError('Only \\" and \\\\ escapes are supported')
                current_element.append(next_char)
                pos += 2
                continue
            elif char == '"':
                in_quotes = False
            else:
                current_element.append(char)
        elif char == '"':
            in_quotes = True
            was_quoted = True
        elif char == "{":
            stack.append([])
        elif char in ",}":
            if current_element or was_quoted:
                elem = "".join(current_element)
                if not was_quoted and elem == "NULL":
                    stack[-1].append(None)
                else:
                    stack[-1].append(_convert_pg_value(elem, elem_oid))
                current_element = []
                was_quoted = False
            if char == "}":
                completed = stack.pop()
                if not stack:
                    return completed
                stack[-1].append(completed)
        elif char != " ":
            current_element.append(char)
        pos += 1

    raise ValueError(f"Malformed array literal: {value}")


# Register standard PostgreSQL types that we'll likely encounter in tests
register_type_info("bool", 16, 1000, lambda v: v == "t")
register_type_info("int2", 21, 1005, int)
register_type_info("int4", 23, 1007, int)
register_type_info("int8", 20, 1016, int)
register_type_info("float4", 700, 1021, float)
register_type_info("float8", 701, 1022, float)
register_type_info("numeric", 1700, 1231, decimal.Decimal)
register_type_info("text", 25, 1009, str)
register_type_info("varchar", 1043, 1015, str)
register_type_info("date", 1082, 1182, datetime.date.fromisoformat)
register_type_info("time", 1083, 1183, datetime.time.fromisoformat)
register_type_info("timestamp", 1114, 1115, datetime.datetime.fromisoformat)
register_type_info("timestamptz", 1184, 1185, datetime.datetime.fromisoformat)
register_type_info("uuid", 2950, 2951, uuid.UUID)
register_type_info("json", 114, 199, json.loads)
register_type_info("jsonb", 3802, 3807, json.loads)


def _convert_pg_value(value: str, type_oid: int) -> Any:
    """
    Convert PostgreSQL string value to appropriate Python type based on OID.
    Uses the registered type converters from register_type_info().
    """
    # Check if it's an array type
    if type_oid in _array_to_elem_map:
        elem_oid = _array_to_elem_map[type_oid]
        return _parse_array(value, elem_oid)

    # Use registered converter if available
    converter = _type_converters.get(type_oid)
    if converter:
        return converter(value)

    # Unknown types - return as string
    return value


def simplify_query_results(results) -> Any:
    """
    Simplify the results of a query so that the caller doesn't have to unpack
    lists and tuples of length 1.
    """
    if len(results) == 1:
        row = results[0]
        if len(row) == 1:
            # If there's only a single cell, just return the value
            return row[0]
        # If there's only a single row, just return that row
        return row

    if len(results) != 0 and len(results[0]) == 1:
        # If there's only a single column, return an array of values
        return [row[0] for row in results]

    # if there are multiple rows and columns, return the results as is
    return results


class PGresult(contextlib.AbstractContextManager):
    """Wraps a raw _PGresult_p with a more friendly interface."""

    def __init__(self, lib: ctypes.CDLL, res: _PGresult_p):
        self._lib = lib
        self._res = res

    def __exit__(self, *exc):
        self._lib.PQclear(self._res)
        self._res = None

    def status(self) -> ExecStatus:
        return ExecStatus(self._lib.PQresultStatus(self._res))

    def error_message(self):
        """Returns the error message associated with this result."""
        msg = self._lib.PQresultErrorMessage(self._res)
        return msg.decode() if msg else ""

    def _get_error_field(self, field: DiagField) -> Optional[str]:
        """Get an error field from the result using PQresultErrorField."""
        val = self._lib.PQresultErrorField(self._res, int(field))
        return val.decode() if val else None

    def raise_error(self) -> None:
        """
        Raises LibpqError with diagnostic information from the result.
        """
        if not self._res:
            raise LibpqError("query failed: out of memory or connection lost")

        sqlstate = self._get_error_field(DiagField.SQLSTATE)
        primary = self._get_error_field(DiagField.MESSAGE_PRIMARY)
        detail = self._get_error_field(DiagField.MESSAGE_DETAIL)
        hint = self._get_error_field(DiagField.MESSAGE_HINT)
        severity = self._get_error_field(DiagField.SEVERITY)
        schema_name = self._get_error_field(DiagField.SCHEMA_NAME)
        table_name = self._get_error_field(DiagField.TABLE_NAME)
        column_name = self._get_error_field(DiagField.COLUMN_NAME)
        datatype_name = self._get_error_field(DiagField.DATATYPE_NAME)
        constraint_name = self._get_error_field(DiagField.CONSTRAINT_NAME)
        context = self._get_error_field(DiagField.CONTEXT)

        position_str = self._get_error_field(DiagField.STATEMENT_POSITION)
        position = int(position_str) if position_str else None

        raise LibpqError(
            primary or self.error_message(),
            sqlstate=sqlstate,
            severity=severity,
            primary=primary,
            detail=detail,
            hint=hint,
            schema_name=schema_name,
            table_name=table_name,
            column_name=column_name,
            datatype_name=datatype_name,
            constraint_name=constraint_name,
            position=position,
            context=context,
        )

    def fetch_all(self):
        """
        Fetch all rows and convert to Python types.
        Returns a list of tuples, with values converted based on their PostgreSQL type.
        """
        nrows = self._lib.PQntuples(self._res)
        ncols = self._lib.PQnfields(self._res)

        # Get type OIDs for each column
        type_oids = [self._lib.PQftype(self._res, col) for col in range(ncols)]

        results = []
        for row in range(nrows):
            row_data = []
            for col in range(ncols):
                if self._lib.PQgetisnull(self._res, row, col):
                    row_data.append(None)
                else:
                    value = self._lib.PQgetvalue(self._res, row, col).decode()
                    row_data.append(_convert_pg_value(value, type_oids[col]))
            results.append(tuple(row_data))

        return results


class PGconn(contextlib.AbstractContextManager):
    """
    Wraps a raw _PGconn_p with a more friendly interface. This is just a
    stub; it's expected to grow.
    """

    def __init__(
        self,
        lib: ctypes.CDLL,
        handle: _PGconn_p,
        stack: contextlib.ExitStack,
    ):
        self._lib = lib
        self._handle = handle
        self._stack = stack

        # Surface NOTICE/WARNING messages (what psql writes to stderr) as Python
        # warnings instead of letting libpq print them, so tests can assert on
        # them with pytest.warns(PostgresWarning/PostgresNotice, ...). The
        # callback object must be kept alive for as long as the connection, or
        # ctypes will free it and libpq will call into freed memory.
        self._notice_cb = _NOTICE_RECEIVER(self._receive_notice)
        self._lib.PQsetNoticeReceiver(self._handle, self._notice_cb, None)

    def _receive_notice(self, _arg, res):
        severity = self._lib.PQresultErrorField(res, int(DiagField.SEVERITY_NONLOCALIZED))
        message = self._lib.PQresultErrorMessage(res)
        # WARNING and NOTICE get their own categories; anything else (INFO, LOG,
        # DEBUG, ...) falls back to the PostgresMessage base.
        category = {
            b"WARNING": PostgresWarning,
            b"NOTICE": PostgresNotice,
        }.get(severity, PostgresMessage)
        warnings.warn(message.decode().rstrip("\n"), category)

    def __exit__(self, *exc):
        self.close()

    def close(self):
        """Close the connection (PQfinish). Idempotent, so it is safe to close
        early — e.g. to disconnect a session deliberately — even though the
        owning ExitStack will also close it at teardown."""
        if self._handle is not None:
            self._lib.PQfinish(self._handle)
            self._handle = None

    def exec(self, query: str):
        """
        Executes a query via PQexec() and returns a PGresult.
        """
        res = self._lib.PQexec(self._handle, query.encode())
        return self._stack.enter_context(PGresult(self._lib, res))

    def sql(self, query: str):
        """
        Executes a query and raises an exception if it fails.
        Returns the query results with automatic type conversion and simplification.
        For commands that don't return data (INSERT, UPDATE, etc.), returns None.

        Examples:
        - SELECT 1 -> 1
        - SELECT 1, 2 -> (1, 2)
        - SELECT * FROM generate_series(1, 3) -> [1, 2, 3]
        - SELECT * FROM (VALUES (1, 'a'), (2, 'b')) t -> [(1, 'a'), (2, 'b')]
        - CREATE TABLE ... -> None
        - INSERT INTO ... -> None
        """
        return self._result_or_raise(self.exec(query))

    def notifies(self):
        """
        Return and consume all pending LISTEN/NOTIFY notifications, each a
        ``Notify(channel, pid, payload)``.

        Input is consumed first (``PQconsumeInput``) so notifications already
        waiting on the socket are picked up. A LISTENing session only receives
        notifications once its transaction ends, so call this after the
        relevant command — and poll, since they may arrive slightly after the
        command's own result.
        """
        self._lib.PQconsumeInput(self._handle)
        out = []
        while True:
            n = self._lib.PQnotifies(self._handle)
            if not n:
                break
            c = n.contents
            out.append(Notify(c.relname.decode(), c.be_pid, c.extra.decode()))
            self._lib.PQfreemem(n)
        return out

    def _result_or_raise(self, res):
        """Turn a PGresult into a simplified Python value, raising LibpqError
        on any error status. Shared by sql() and the extended-protocol helpers."""
        status = res.status()
        if status == ExecStatus.PGRES_COMMAND_OK:
            return None
        if status == ExecStatus.PGRES_TUPLES_OK:
            return simplify_query_results(res.fetch_all())
        if status == ExecStatus.PGRES_COPY_OUT:
            # Drain the COPY OUT stream. The rows are discarded, but draining
            # is what surfaces an error raised mid-copy (e.g. a permission or
            # buffer-access failure): PQgetCopyData returns -2 and the real
            # result then comes from PQgetResult.
            buf = ctypes.c_char_p()
            while self._lib.PQgetCopyData(self._handle, ctypes.byref(buf), 0) > 0:
                if buf:
                    self._lib.PQfreemem(buf)
                buf = ctypes.c_char_p()
            final = self._lib.PQgetResult(self._handle)
            return self._result_or_raise(
                self._stack.enter_context(PGresult(self._lib, final))
            )
        res.raise_error()

    def exec_params(self, query: str, *params):
        """
        Run ``query`` through the extended protocol, binding ``params`` to its
        ``$1, $2, ...`` placeholders in text format (an unnamed Parse/Bind/
        Execute). This is the libpq equivalent of psql's
        ``<query> \\bind <params> \\g``. Raises on error, otherwise returns the
        simplified results like sql().
        """
        nparams, values = _build_params(params)
        res = self._lib.PQexecParams(
            self._handle, query.encode(), nparams, None, values, None, None, 0
        )
        return self._result_or_raise(self._stack.enter_context(PGresult(self._lib, res)))

    def prepare(self, name: str, query: str):
        """
        Parse ``query`` into a named prepared statement. This is the libpq
        equivalent of psql's ``<query> \\parse <name>``. Execute it afterwards
        with exec_prepared().
        """
        res = self._lib.PQprepare(
            self._handle, name.encode(), query.encode(), 0, None
        )
        return self._result_or_raise(self._stack.enter_context(PGresult(self._lib, res)))

    def exec_prepared(self, name: str, *params):
        """
        Bind ``params`` to a previously prepare()d statement and execute it.
        This is the libpq equivalent of psql's
        ``\\bind_named <name> <params> \\g``.
        """
        nparams, values = _build_params(params)
        res = self._lib.PQexecPrepared(
            self._handle, name.encode(), nparams, values, None, None, 0
        )
        return self._result_or_raise(self._stack.enter_context(PGresult(self._lib, res)))


def connstr(opts: Dict[str, Any]) -> str:
    """
    Flattens the provided options into a libpq connection string. Values
    are converted to str and quoted/escaped as necessary.
    """
    settings = []

    for k, v in opts.items():
        v = str(v)
        if not v:
            v = "''"
        else:
            v = v.replace("\\", "\\\\")
            v = v.replace("'", "\\'")

            if " " in v:
                v = f"'{v}'"

        settings.append(f"{k}={v}")

    return " ".join(settings)


def connect(
    libpq_handle: ctypes.CDLL,
    stack: contextlib.ExitStack,
    remaining_timeout_fn: Callable[[], float],
    **opts,
) -> PGconn:
    """
    Connects to a server, using the given connection options, and
    returns a PGconn object wrapping the connection handle. A
    failure will raise LibpqError.

    Connections honor PG_TEST_TIMEOUT_DEFAULT unless connect_timeout is
    explicitly overridden in opts.

    Args:
        libpq_handle: ctypes.CDLL handle to libpq library
        stack: ExitStack for managing connection cleanup
        remaining_timeout_fn: Function that returns remaining timeout in seconds
        **opts: Connection options (host, port, dbname, etc.)

    Returns:
        PGconn: Connected database connection

    Raises:
        LibpqError: If connection fails
    """

    if "connect_timeout" not in opts:
        t = int(remaining_timeout_fn())
        opts["connect_timeout"] = max(t, 1)

    conn_p = libpq_handle.PQconnectdb(connstr(opts).encode())

    # Check connection status before adding to stack
    if libpq_handle.PQstatus(conn_p) != ConnectionStatus.CONNECTION_OK:
        error_msg = libpq_handle.PQerrorMessage(conn_p).decode()
        # Manually close the failed connection
        libpq_handle.PQfinish(conn_p)
        raise LibpqError(error_msg)

    # Connection succeeded - add to stack for cleanup
    conn = stack.enter_context(PGconn(libpq_handle, conn_p, stack=stack))
    return conn
