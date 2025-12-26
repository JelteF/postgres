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
from typing import Any, Callable, Dict, Optional

from .errors import LibpqError, make_error


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


class PollingStatus(enum.IntEnum):
    """PostgreSQL polling status codes from PQconnectPoll/PQcancelPoll."""

    PGRES_POLLING_FAILED = 0
    PGRES_POLLING_READING = 1
    PGRES_POLLING_WRITING = 2
    PGRES_POLLING_OK = 3


class _PGconn(ctypes.Structure):
    pass


class _PGresult(ctypes.Structure):
    pass


class _PGcancel(ctypes.Structure):
    pass


class _PGcancelConn(ctypes.Structure):
    pass


_PGconn_p = ctypes.POINTER(_PGconn)
_PGresult_p = ctypes.POINTER(_PGresult)
_PGcancel_p = ctypes.POINTER(_PGcancel)
_PGcancelConn_p = ctypes.POINTER(_PGcancelConn)


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
        # On Windows, libpq.dll is confusingly in bindir, not libdir. And we
        # need to add this directory the the search path.
        libpq_path = os.path.join(bindir, name)
        lib = ctypes.CDLL(libpq_path)
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

    # Async query functions
    lib.PQsendQueryParams.restype = ctypes.c_int
    lib.PQsendQueryParams.argtypes = [
        _PGconn_p,
        ctypes.c_char_p,  # command
        ctypes.c_int,  # nParams
        ctypes.POINTER(ctypes.c_uint),  # paramTypes
        ctypes.POINTER(ctypes.c_char_p),  # paramValues
        ctypes.POINTER(ctypes.c_int),  # paramLengths
        ctypes.POINTER(ctypes.c_int),  # paramFormats
        ctypes.c_int,  # resultFormat
    ]

    lib.PQgetResult.restype = _PGresult_p
    lib.PQgetResult.argtypes = [_PGconn_p]

    lib.PQconsumeInput.restype = ctypes.c_int
    lib.PQconsumeInput.argtypes = [_PGconn_p]

    lib.PQisBusy.restype = ctypes.c_int
    lib.PQisBusy.argtypes = [_PGconn_p]

    lib.PQsetnonblocking.restype = ctypes.c_int
    lib.PQsetnonblocking.argtypes = [_PGconn_p, ctypes.c_int]

    lib.PQisnonblocking.restype = ctypes.c_int
    lib.PQisnonblocking.argtypes = [_PGconn_p]

    lib.PQbackendPID.restype = ctypes.c_int
    lib.PQbackendPID.argtypes = [_PGconn_p]

    lib.PQsocket.restype = ctypes.c_int
    lib.PQsocket.argtypes = [_PGconn_p]

    # Legacy cancel functions (PGcancel)
    lib.PQgetCancel.restype = _PGcancel_p
    lib.PQgetCancel.argtypes = [_PGconn_p]

    lib.PQfreeCancel.restype = None
    lib.PQfreeCancel.argtypes = [_PGcancel_p]

    lib.PQcancel.restype = ctypes.c_int
    lib.PQcancel.argtypes = [_PGcancel_p, ctypes.c_char_p, ctypes.c_int]

    lib.PQrequestCancel.restype = ctypes.c_int
    lib.PQrequestCancel.argtypes = [_PGconn_p]

    # Modern cancel functions (PGcancelConn)
    lib.PQcancelCreate.restype = _PGcancelConn_p
    lib.PQcancelCreate.argtypes = [_PGconn_p]

    lib.PQcancelBlocking.restype = ctypes.c_int
    lib.PQcancelBlocking.argtypes = [_PGcancelConn_p]

    lib.PQcancelStart.restype = ctypes.c_int
    lib.PQcancelStart.argtypes = [_PGcancelConn_p]

    lib.PQcancelPoll.restype = ctypes.c_int
    lib.PQcancelPoll.argtypes = [_PGcancelConn_p]

    lib.PQcancelSocket.restype = ctypes.c_int
    lib.PQcancelSocket.argtypes = [_PGcancelConn_p]

    lib.PQcancelStatus.restype = ctypes.c_int
    lib.PQcancelStatus.argtypes = [_PGcancelConn_p]

    lib.PQcancelErrorMessage.restype = ctypes.c_char_p
    lib.PQcancelErrorMessage.argtypes = [_PGcancelConn_p]

    lib.PQcancelReset.restype = None
    lib.PQcancelReset.argtypes = [_PGcancelConn_p]

    lib.PQcancelFinish.restype = None
    lib.PQcancelFinish.argtypes = [_PGcancelConn_p]

    return lib


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
        Raises an appropriate LibpqError subclass based on the error fields.
        Extracts SQLSTATE and other diagnostic information from the result.
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

        raise make_error(
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


class PGcancel(contextlib.AbstractContextManager):
    """Wraps a raw _PGcancel_p with a more friendly interface."""

    def __init__(self, lib: ctypes.CDLL, cancel: _PGcancel_p):
        self._lib = lib
        self._cancel = cancel

    def __exit__(self, *exc):
        if self._cancel:
            self._lib.PQfreeCancel(self._cancel)
            self._cancel = None

    def cancel(self) -> None:
        """Send a cancel request. Raises LibpqError on failure."""
        errbuf = ctypes.create_string_buffer(256)
        if not self._lib.PQcancel(self._cancel, errbuf, len(errbuf)):
            raise LibpqError(errbuf.value.decode())


class PGcancelConn(contextlib.AbstractContextManager):
    """Wraps a raw _PGcancelConn_p with a more friendly interface."""

    def __init__(self, lib: ctypes.CDLL, cancel_conn: _PGcancelConn_p):
        self._lib = lib
        self._cancel_conn = cancel_conn

    def __exit__(self, *exc):
        if self._cancel_conn:
            self._lib.PQcancelFinish(self._cancel_conn)
            self._cancel_conn = None

    def blocking(self) -> bool:
        """Send a cancel request and block until complete. Returns True on success."""
        return bool(self._lib.PQcancelBlocking(self._cancel_conn))

    def start(self) -> bool:
        """Start an asynchronous cancel request. Returns True on success."""
        return bool(self._lib.PQcancelStart(self._cancel_conn))

    def poll(self) -> PollingStatus:
        """Poll the cancel connection. Returns the polling status."""
        return PollingStatus(self._lib.PQcancelPoll(self._cancel_conn))

    def socket(self) -> int:
        """Get the socket file descriptor."""
        return self._lib.PQcancelSocket(self._cancel_conn)

    def status(self) -> ConnectionStatus:
        """Get the cancel connection status."""
        return ConnectionStatus(self._lib.PQcancelStatus(self._cancel_conn))

    def error_message(self) -> str:
        """Get the error message, if any."""
        msg = self._lib.PQcancelErrorMessage(self._cancel_conn)
        return msg.decode() if msg else ""

    def reset(self) -> None:
        """Reset the cancel connection for reuse."""
        self._lib.PQcancelReset(self._cancel_conn)

    def poll_until_ready(self, timeout: float = 3.0) -> None:
        """
        Poll the cancel connection until it completes.
        Raises LibpqError on failure.
        """
        import select

        while True:
            poll_status = self.poll()

            if poll_status == PollingStatus.PGRES_POLLING_OK:
                return
            if poll_status == PollingStatus.PGRES_POLLING_FAILED:
                raise LibpqError(f"cancel failed: {self.error_message()}")

            sock = self.socket()
            if sock < 0:
                raise LibpqError(f"invalid socket: {self.error_message()}")

            if poll_status == PollingStatus.PGRES_POLLING_READING:
                select.select([sock], [], [], timeout)
            elif poll_status == PollingStatus.PGRES_POLLING_WRITING:
                select.select([], [sock], [], timeout)


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

    def __exit__(self, *exc):
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
        res = self.exec(query)
        status = res.status()

        if status == ExecStatus.PGRES_FATAL_ERROR:
            res.raise_error()
        elif status == ExecStatus.PGRES_COMMAND_OK:
            return None
        elif status == ExecStatus.PGRES_TUPLES_OK:
            results = res.fetch_all()
            return simplify_query_results(results)
        else:
            res.raise_error()

    def set_nonblocking(self, nonblocking: bool) -> None:
        """Set the connection to nonblocking mode."""
        if self._lib.PQsetnonblocking(self._handle, int(nonblocking)) != 0:
            raise LibpqError(f"failed to set nonblocking mode: {self.error_message()}")

    def is_nonblocking(self) -> bool:
        """Check if the connection is in nonblocking mode."""
        return bool(self._lib.PQisnonblocking(self._handle))

    def backend_pid(self) -> int:
        """Get the backend process ID."""
        return self._lib.PQbackendPID(self._handle)

    def socket(self) -> int:
        """Get the socket file descriptor."""
        return self._lib.PQsocket(self._handle)

    def error_message(self) -> str:
        """Get the error message, if any."""
        msg = self._lib.PQerrorMessage(self._handle)
        return msg.decode() if msg else ""

    def send_query_params(
        self, command: str, params: Optional[list] = None
    ) -> bool:
        """
        Send a query with parameters asynchronously.
        Returns True on success, False on failure.
        """
        if params is None:
            params = []

        nparams = len(params)

        # Convert params to c_char_p array
        if nparams > 0:
            param_values = (ctypes.c_char_p * nparams)()
            for i, p in enumerate(params):
                if p is None:
                    param_values[i] = None
                else:
                    param_values[i] = str(p).encode()
        else:
            param_values = None

        result = self._lib.PQsendQueryParams(
            self._handle,
            command.encode(),
            nparams,
            None,  # paramTypes - let the server infer
            param_values,
            None,  # paramLengths
            None,  # paramFormats
            0,  # resultFormat - text
        )
        return bool(result)

    def get_result(self) -> Optional[PGresult]:
        """
        Get the next result from an async query.
        Returns None when there are no more results.
        """
        res = self._lib.PQgetResult(self._handle)
        if not res:
            return None
        return self._stack.enter_context(PGresult(self._lib, res))

    def consume_input(self) -> bool:
        """Consume input from the server. Returns True on success."""
        return bool(self._lib.PQconsumeInput(self._handle))

    def is_busy(self) -> bool:
        """Check if the connection is busy waiting for results."""
        return bool(self._lib.PQisBusy(self._handle))

    def get_cancel(self) -> PGcancel:
        """Get a PGcancel object for this connection."""
        cancel = self._lib.PQgetCancel(self._handle)
        if not cancel:
            raise LibpqError("failed to get cancel object")
        return self._stack.enter_context(PGcancel(self._lib, cancel))

    def request_cancel(self) -> bool:
        """Request cancellation of the current query. Returns True on success."""
        return bool(self._lib.PQrequestCancel(self._handle))

    def cancel_create(self) -> PGcancelConn:
        """Create a PGcancelConn object for this connection."""
        cancel_conn = self._lib.PQcancelCreate(self._handle)
        if not cancel_conn:
            raise LibpqError("failed to create cancel connection")
        return self._stack.enter_context(PGcancelConn(self._lib, cancel_conn))


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
