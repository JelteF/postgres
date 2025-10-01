# Copyright (c) 2025, PostgreSQL Global Development Group

import contextlib
import ctypes
import platform
import time
from typing import Any, Callable, Dict

import pytest

from ._env import test_timeout_default


@pytest.fixture
def remaining_timeout():
    """
    This fixture provides a function that returns how much of the
    PG_TEST_TIMEOUT_DEFAULT remains for the current test, in fractional seconds.
    This value is never less than zero.

    This fixture is per-test, so the deadline is also reset on a per-test basis.
    """
    now = time.monotonic()
    deadline = now + test_timeout_default()

    return lambda: max(deadline - time.monotonic(), 0)


class _PGconn(ctypes.Structure):
    pass


class _PGresult(ctypes.Structure):
    pass


_PGconn_p = ctypes.POINTER(_PGconn)
_PGresult_p = ctypes.POINTER(_PGresult)


@pytest.fixture(scope="session")
def libpq_handle():
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

    # XXX ctypes.CDLL() is a little stricter with load paths on Windows. The
    # preferred way around that is to know the absolute path to libpq.dll, but
    # that doesn't seem to mesh well with the current test infrastructure. For
    # now, enable "standard" LoadLibrary behavior.
    loadopts = {}
    if system == "Windows":
        loadopts["winmode"] = 0

    lib = ctypes.CDLL(name, **loadopts)

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

    lib.PQresultErrorMessage.restype = ctypes.c_char_p
    lib.PQresultErrorMessage.argtypes = [_PGresult_p]

    lib.PQprepare.restype = _PGresult_p
    lib.PQprepare.argtypes = [_PGconn_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]

    lib.PQexecPrepared.restype = _PGresult_p
    lib.PQexecPrepared.argtypes = [_PGconn_p, ctypes.c_char_p, ctypes.c_int, ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.c_int]

    lib.PQnfields.restype = ctypes.c_int
    lib.PQnfields.argtypes = [_PGresult_p]

    lib.PQfname.restype = ctypes.c_char_p
    lib.PQfname.argtypes = [_PGresult_p, ctypes.c_int]

    lib.PQftype.restype = ctypes.c_uint
    lib.PQftype.argtypes = [_PGresult_p, ctypes.c_int]

    # Extended prepared statement API
    lib.PQprepareExt.restype = ctypes.c_void_p  # PGpreparedStmt*
    lib.PQprepareExt.argtypes = [_PGconn_p, ctypes.c_char_p, ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]

    lib.PQexecPreparedExt.restype = _PGresult_p
    lib.PQexecPreparedExt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.c_int]

    lib.PQclosePreparedExt.restype = None
    lib.PQclosePreparedExt.argtypes = [ctypes.c_void_p]

    lib.PQpreparedNfields.restype = ctypes.c_int
    lib.PQpreparedNfields.argtypes = [ctypes.c_void_p]

    lib.PQpreparedFname.restype = ctypes.c_char_p
    lib.PQpreparedFname.argtypes = [ctypes.c_void_p, ctypes.c_int]

    lib.PQpreparedFtype.restype = ctypes.c_uint
    lib.PQpreparedFtype.argtypes = [ctypes.c_void_p, ctypes.c_int]

    lib.PQfinish.restype = None
    lib.PQfinish.argtypes = [_PGconn_p]

    return lib


class PGresult(contextlib.AbstractContextManager):
    """Wraps a raw _PGresult_p with a more friendly interface."""

    def __init__(self, lib: ctypes.CDLL, res: _PGresult_p):
        self._lib = lib
        self._res = res

    def __exit__(self, *exc):
        self._lib.PQclear(self._res)
        self._res = None

    def status(self):
        return self._lib.PQresultStatus(self._res)

    def error_message(self):
        return self._lib.PQresultErrorMessage(self._res).decode()

    def nfields(self):
        return self._lib.PQnfields(self._res)

    def fname(self, field_num):
        return self._lib.PQfname(self._res, field_num).decode()

    def ftype(self, field_num):
        return self._lib.PQftype(self._res, field_num)


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

    def exec(self, query: str) -> PGresult:
        """
        Executes a query via PQexec() and returns a PGresult.
        """
        res = self._lib.PQexec(self._handle, query.encode())
        return self._stack.enter_context(PGresult(self._lib, res))

    def prepare(self, stmt_name: str, query: str) -> PGresult:
        """
        Prepares a statement via PQprepare() and returns a PGresult.
        """
        res = self._lib.PQprepare(self._handle, stmt_name.encode(), query.encode(), 0, None)
        return self._stack.enter_context(PGresult(self._lib, res))

    def exec_prepared(self, stmt_name: str) -> PGresult:
        """
        Executes a prepared statement via PQexecPrepared() and returns a PGresult.
        Simple version with no parameters.
        """
        res = self._lib.PQexecPrepared(self._handle, stmt_name.encode(), 0, None, None, None, 0)
        return self._stack.enter_context(PGresult(self._lib, res))

    def prepare_ext(self, query: str):
        """
        Prepares a statement via PQprepareExt() and returns a handle.
        This is registered with the stack for automatic cleanup.
        """
        stmt = self._lib.PQprepareExt(self._handle, query.encode(), 0, None)
        if not stmt:
            return None
        return self._stack.enter_context(PGpreparedStmtExt(self._lib, stmt))


class PGpreparedStmtExt(contextlib.AbstractContextManager):
    """Wraps a PGpreparedStmt* handle from PQprepareExt."""

    def __init__(self, lib: ctypes.CDLL, handle: ctypes.c_void_p):
        self._lib = lib
        self._handle = handle

    def __exit__(self, *exc):
        self._lib.PQclosePreparedExt(self._handle)
        self._handle = None

    def exec(self) -> PGresult:
        """
        Executes the prepared statement via PQexecPreparedExt().
        Simple version with no parameters.
        """
        res = self._lib.PQexecPreparedExt(self._handle, 0, None, None, None, 0)
        # Note: We don't use enter_context here because the result is standalone
        return PGresult(self._lib, res)

    def nfields(self):
        return self._lib.PQpreparedNfields(self._handle)

    def fname(self, field_num):
        result = self._lib.PQpreparedFname(self._handle, field_num)
        return result.decode() if result else None

    def ftype(self, field_num):
        return self._lib.PQpreparedFtype(self._handle, field_num)


@pytest.fixture
def libpq(libpq_handle, remaining_timeout):
    """
    Provides a ctypes-based API wrapped around libpq.so. This fixture keeps
    track of allocated resources and cleans them up during teardown. See
    _Libpq's public API for details.
    """

    class _Libpq(contextlib.ExitStack):
        CONNECTION_OK = 0

        PGRES_EMPTY_QUERY = 0
        PGRES_COMMAND_OK = 1
        PGRES_TUPLES_OK = 2

        class Error(RuntimeError):
            """
            libpq.Error is the exception class for application-level errors that
            are encountered during libpq operations.
            """

            pass

        def __init__(self):
            super().__init__()
            self.lib = libpq_handle

        def _connstr(self, opts: Dict[str, Any]) -> str:
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

        def must_connect(self, **opts) -> PGconn:
            """
            Connects to a server, using the given connection options, and
            returns a libpq.PGconn object wrapping the connection handle. A
            failure will raise libpq.Error.

            Connections honor PG_TEST_TIMEOUT_DEFAULT unless connect_timeout is
            explicitly overridden in opts.
            """

            if "connect_timeout" not in opts:
                t = int(remaining_timeout())
                opts["connect_timeout"] = max(t, 1)

            conn_p = self.lib.PQconnectdb(self._connstr(opts).encode())

            # Ensure the connection handle is always closed at the end of the
            # test.
            conn = self.enter_context(PGconn(self.lib, conn_p, stack=self))

            if self.lib.PQstatus(conn_p) != self.CONNECTION_OK:
                raise self.Error(self.lib.PQerrorMessage(conn_p).decode())

            return conn

    with _Libpq() as lib:
        yield lib
