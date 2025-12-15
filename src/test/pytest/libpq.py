# Copyright (c) 2025, PostgreSQL Global Development Group

"""
libpq testing utilities - ctypes bindings and helpers for PostgreSQL's libpq library.

This module provides Python wrappers around libpq for use in pytest tests.
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
from typing import Any, Callable, Dict


class LibpqError(RuntimeError):
    """
    Exception class for application-level errors that are encountered during libpq operations.
    """

    pass


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


_PGconn_p = ctypes.POINTER(_PGconn)
_PGresult_p = ctypes.POINTER(_PGresult)


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


# Helper converters
def _parse_array(value: str, elem_oid: int) -> list:
    """Parse PostgreSQL array syntax: {elem1,elem2,elem3}"""
    if not (value.startswith("{") and value.endswith("}")):
        return value

    inner = value[1:-1]
    if not inner:
        return []

    elements = inner.split(",")
    result = []
    for elem in elements:
        elem = elem.strip()
        if elem == "NULL":
            result.append(None)
        else:
            # Remove quotes if present
            if elem.startswith('"') and elem.endswith('"'):
                elem = elem[1:-1]
            result.append(_convert_pg_value(elem, elem_oid))

    return result


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
            error_msg = res.error_message()
            raise LibpqError(f"Query failed: {error_msg}\nQuery: {query}")
        elif status == ExecStatus.PGRES_COMMAND_OK:
            return None
        elif status == ExecStatus.PGRES_TUPLES_OK:
            results = res.fetch_all()
            return simplify_query_results(results)
        else:
            error_msg = res.error_message() or f"Unexpected status: {status}"
            raise LibpqError(f"Query failed: {error_msg}\nQuery: {query}")


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
