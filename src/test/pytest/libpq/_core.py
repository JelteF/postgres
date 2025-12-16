# Copyright (c) 2025, PostgreSQL Global Development Group

"""
Friendly connection and result wrappers over libpq.

``PGconn`` and ``PGresult`` wrap the raw libpq handles from ``_bindings`` and
turn them into a Pythonic API (``sql()``, ``background_sql()``, ``notifies()``,
...). The ctypes bindings live in ``_bindings``, value conversion in
``_conversions``, and the server-message warning categories in ``messages``;
this module ties them together.
"""

from __future__ import annotations

import contextlib
import ctypes
import warnings
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, NamedTuple, NoReturn

from ._bindings import (
    ConnectionStatus,
    DiagField,
    ExecStatus,
    _NOTICE_RECEIVER,
    _PGconn,
    _PGresult,
    _extract_diag_fields,
)
from ._conversions import _build_params, _convert_pg_value, simplify_query_results
from .errors import (
    LibpqError,
    PostgresMessage,
    PostgresNotice,
    PostgresWarning,
)


# A LISTEN/NOTIFY notification, as returned by PGconn.notifies().
class Notify(NamedTuple):
    channel: str
    pid: int
    payload: str


class PGresult(contextlib.AbstractContextManager):
    """Wraps a raw _PGresult_p with a more friendly interface."""

    def __init__(self, lib: ctypes.CDLL, res: ctypes._Pointer[_PGresult]):
        self._lib = lib
        # Cleared to None by __exit__ once the result has been freed.
        self._res: ctypes._Pointer[_PGresult] | None = res

    def __exit__(self, *exc: object) -> None:
        self._lib.PQclear(self._res)
        self._res = None

    def status(self) -> ExecStatus:
        return ExecStatus(self._lib.PQresultStatus(self._res))

    def error_message(self) -> str:
        """Returns the error message associated with this result."""
        msg = self._lib.PQresultErrorMessage(self._res)
        return msg.decode() if msg else ""

    def raise_error(self) -> NoReturn:
        """
        Raises LibpqError with diagnostic information from the result.
        """
        if not self._res:
            raise LibpqError("query failed: out of memory or connection lost")

        fields = _extract_diag_fields(self._lib, self._res)
        raise LibpqError(fields["primary"] or self.error_message(), **fields)

    def fetch_all(self) -> list[tuple[Any, ...]]:
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


class _MustConsumeFuture(Future):
    """The Future returned by ``PGconn.background_sql()``.

    A plain ``concurrent.futures.Future`` silently drops an exception that
    nobody retrieves — unlike ``asyncio`` it does not even warn — which would
    let a background query fail unnoticed. Python has no ``[[nodiscard]]`` /
    ``warn_unused_result`` to require otherwise, so this subclass records
    whether its outcome was consumed (via ``result()``/``exception()``).
    ``PGconn.close()`` raises if a query's result was never consumed, turning a
    forgotten ``.result()`` into a visible test failure instead of a silent
    pass.
    """

    def __init__(self) -> None:
        super().__init__()
        self.consumed = False

    def result(self, timeout: float | None = None) -> Any:
        self.consumed = True
        return super().result(timeout)

    def exception(self, timeout: float | None = None) -> BaseException | None:
        self.consumed = True
        return super().exception(timeout)


class PGconn(contextlib.AbstractContextManager):
    """
    Wraps a raw _PGconn_p with a more friendly interface. This is just a
    stub; it's expected to grow.
    """

    def __init__(
        self,
        lib: ctypes.CDLL,
        handle: ctypes._Pointer[_PGconn],
        stack: contextlib.ExitStack,
    ):
        self._lib = lib
        # Cleared to None by close() once the connection has been finished.
        self._handle: ctypes._Pointer[_PGconn] | None = handle
        self._stack = stack

        # background_sql() machinery. A single libpq connection must never be
        # driven by two threads at once, so background queries run on one
        # worker thread (created lazily on first use) and only one may be in
        # flight at a time. ``_pending`` is that query's future, if any.
        self._executor: ThreadPoolExecutor | None = None
        self._pending: _MustConsumeFuture | None = None

        # Sequence for prepare()'s generated __p{n} statement names.
        self._prepared_seq = 0

        # Surface NOTICE/WARNING messages (what psql writes to stderr) as Python
        # warnings instead of letting libpq print them, so tests can assert on
        # them with pytest.warns(PostgresWarning/PostgresNotice, ...). The
        # callback object must be kept alive for as long as the connection, or
        # ctypes will free it and libpq will call into freed memory.
        self._notice_cb = _NOTICE_RECEIVER(self._receive_notice)
        self._lib.PQsetNoticeReceiver(self._handle, self._notice_cb, None)

    def _receive_notice(
        self, _arg: int | None, res: ctypes._Pointer[_PGresult]
    ) -> None:
        severity = self._lib.PQresultErrorField(
            res, int(DiagField.SEVERITY_NONLOCALIZED)
        )
        message = self._lib.PQresultErrorMessage(res)
        # WARNING and NOTICE get their own categories; anything else (INFO, LOG,
        # DEBUG, ...) falls back to the PostgresMessage base.
        category = {
            b"WARNING": PostgresWarning,
            b"NOTICE": PostgresNotice,
        }.get(severity, PostgresMessage)
        # Attach the same diagnostic fields raise_error() puts on a LibpqError,
        # so a caught notice exposes .detail/.hint/.sqlstate/etc. Passing a
        # constructed warning instance (rather than a string + category) makes
        # warnings.warn use the instance's type as the category.
        fields = _extract_diag_fields(self._lib, res)
        warnings.warn(category(message.decode().rstrip("\n"), **fields))

    def __exit__(self, *exc: object) -> None:
        # If we're being closed while another exception is already propagating,
        # that exception is the real failure: abandon any pending background
        # query and release resources without raising close()'s own "result
        # never consumed" (or "still in flight") error on top of it.
        if exc[0] is not None:
            self._pending = None
        self.close()

    def close(self) -> None:
        """Close the connection (PQfinish). Idempotent, so it is safe to close
        early — e.g. to disconnect a session deliberately — even though the
        owning ExitStack will also close it at teardown.

        Like every other query method this first runs _check_pending(), so
        closing a connection with an unconsumed background_sql() raises
        RuntimeError: consume its future (call .result()) before closing. Once
        that guard passes the future has been consumed, so the worker thread
        has finished and joining it (rather than cancelling) before PQfinish
        never races libpq."""

        self._check_pending()

        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None

        self._close_impl()

    def _close_impl(self) -> None:
        """Release the libpq handle (PQfinish), without close()'s pending
        guard or executor shutdown. Idempotent; also called directly by the
        background_sql(close_when_done=True) worker, which cannot go through
        close() (see background_sql)."""
        if self._handle is not None:
            self._lib.PQfinish(self._handle)
            self._handle = None

    def close_portal(self, name: str) -> None:
        """
        Close a portal, releasing its snapshot. Pass ``""`` for the unnamed
        portal — the one sql()/sql_batch() statements bind.

        After a sql()/sql_batch() statement inside an open transaction, the
        unnamed portal created by its Bind survives until the next statement
        or the end of the transaction, keeping the statement's snapshot
        registered and therefore the backend's xmin set. Call this when a test
        needs an idle-in-transaction session that holds an XID but no snapshot
        — e.g. one that must NOT be waited on by CREATE INDEX CONCURRENTLY's
        WaitForOlderSnapshots.
        """
        self._check_pending()
        res = self._lib.PQclosePortal(self._handle, name.encode())
        self._result_or_raise(self._stack.enter_context(PGresult(self._lib, res)))

    def sql(self, query: str, *params: Any) -> Any:
        """
        Runs ``query`` through the extended query protocol (an unnamed Parse/
        Bind/Execute), the same path real client drivers use, and raises an
        exception if it fails. Any ``params`` are bound to the query's
        ``$1, $2, ...`` placeholders in text format, the libpq equivalent of
        psql's ``<query> \\bind <params> \\g``.

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
        self._check_pending()
        return self._sql_impl(query, *params)

    def _sql_impl(self, query: str, *params: Any) -> Any:
        """The actual PQexecParams call behind sql()/background_sql(). Runs on
        the caller's thread for sql() and on the worker thread for
        background_sql(); _check_pending() guarantees only one of those touches
        the connection at a time."""
        nparams, values = _build_params(params)
        res = self._lib.PQexecParams(
            self._handle, query.encode(), nparams, None, values, None, None, 0
        )
        return self._result_or_raise(
            self._stack.enter_context(PGresult(self._lib, res))
        )

    def _check_pending(self) -> None:
        """Guard run before anything else touches the connection. A
        background_sql() future must be consumed — its result()/exception()
        retrieved — before the connection is reused; until then this raises.

        While the query is still running the connection is genuinely busy: a
        real session can't run two queries at once, and a second libpq call
        would race the worker thread. Once it has finished but its result is
        still uncollected, raising forces the caller to deal with the outcome
        here (including any error) rather than letting it silently leak past
        the next query. Either way the fix is the same: call .result() on the
        future. Once the future has been consumed, forget it."""
        if self._pending is None:
            return

        if self._pending.consumed:
            self._pending = None
            return

        if self._pending.done():
            raise RuntimeError(
                "the previous background_sql() result was never "
                "consumed; call .result() on its future before "
                "issuing another query"
            )
        raise RuntimeError(
            "connection is busy with an unresolved background_sql(); "
            "call .result() on its future before issuing another query"
        )

    def background_sql(
        self, query: str, *params: Any, close_when_done: bool = False
    ) -> Future[Any]:
        """Dispatch a query that is expected to *block* — on a lock, an
        injection point, or anything else that won't return promptly — and
        return a Future that is already running. The test can carry on (e.g.
        confirm the wait with ``PostgresServer.wait_for_event()``, then release
        it) and call ``.result()`` on the future to collect the outcome once it
        unblocks; ``.result()`` re-raises any LibpqError.

        This is the replacement for Perl's ``background_psql``. Unlike Perl it
        does not spawn a psql subprocess: the query runs on a worker thread
        over this same connection, so the session state it builds up (open
        transactions, held locks, session-local settings) is visible to later
        sql()/background_sql() calls on this connection, just like a real
        session. Only one background query at a time: its future must be
        consumed (call .result()) before another query runs on this
        connection, or _check_pending() raises.

        With ``close_when_done=True`` the worker thread finishes the connection
        (PQfinish) as soon as the query completes, for callers that dispatch on
        a throwaway connection and only care about the future (see
        ``PostgresServer.background_sql_oneshot``). The worker cannot go
        through close() for this — close() rejects an unconsumed future and
        would join the worker's own thread — so it releases the libpq handle
        directly; the must-consume guard still fires at teardown, when the
        cleanup stack calls close() on the (already finished) connection."""
        return self._dispatch_background(
            lambda: self._sql_impl(query, *params), close_when_done=close_when_done
        )

    def _dispatch_background(
        self, work: Any, *, close_when_done: bool = False
    ) -> Future[Any]:
        """Run ``work`` (a no-argument callable issuing exactly one query on
        this connection) on the worker thread, with the single-pending-query
        bookkeeping shared by background_sql() and
        PreparedStatement.background_exec()."""
        self._check_pending()
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1)
        fut = _MustConsumeFuture()

        def run():
            if not fut.set_running_or_notify_cancel():
                return
            try:
                fut.set_result(work())
            except BaseException as e:  # noqa: BLE001 - hand every error to the future
                fut.set_exception(e)
            finally:
                if close_when_done:
                    self._close_impl()

        self._executor.submit(run)
        self._pending = fut
        return fut

    def sql_batch(self, *queries: str) -> list[Any]:
        """
        Runs each of ``queries`` through the extended query protocol, exactly
        like consecutive sql() calls, and returns a list with every statement's
        simplified result.

        Raises on the first failing statement; earlier statements stay
        executed (and committed).
        """
        self._check_pending()
        return [self._sql_impl(query) for query in queries]

    def notifies(self) -> list[Notify]:
        """
        Return and consume all pending LISTEN/NOTIFY notifications, each a
        ``Notify(channel, pid, payload)``.

        Input is consumed first (``PQconsumeInput``) so notifications already
        waiting on the socket are picked up. A LISTENing session only receives
        notifications once its transaction ends, so call this after the
        relevant command — and poll, since they may arrive slightly after the
        command's own result.
        """
        self._check_pending()
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

    def _result_or_raise(self, res: PGresult) -> Any:
        """Turn a PGresult into a simplified Python value, raising LibpqError
        on any error status. Shared by sql() and the extended-protocol helpers."""
        status = res.status()
        if status == ExecStatus.PGRES_COMMAND_OK:
            return None
        if status == ExecStatus.PGRES_TUPLES_OK:
            return simplify_query_results(res.fetch_all())
        if status == ExecStatus.PGRES_COPY_OUT:
            # Drain the COPY OUT stream, collecting the rows. Draining is also
            # what surfaces an error raised mid-copy (e.g. a permission or
            # buffer-access failure): PQgetCopyData returns -2 and the real
            # result then comes from PQgetResult, so we resolve that result
            # (raising on error) before handing back the copied bytes. The
            # bytes are returned undecoded since COPY can stream binary data.
            chunks = []
            buf = ctypes.c_char_p()
            while True:
                # Each call hands back one row in a freshly malloc'd buffer and
                # returns its length; read exactly that many bytes since COPY
                # data is not guaranteed to be NUL-terminated.
                n = self._lib.PQgetCopyData(self._handle, ctypes.byref(buf), 0)
                if n <= 0:
                    break
                chunks.append(ctypes.string_at(buf, n))
                self._lib.PQfreemem(buf)
                buf = ctypes.c_char_p()
            final = self._lib.PQgetResult(self._handle)
            self._result_or_raise(self._stack.enter_context(PGresult(self._lib, final)))
            return b"".join(chunks)
        res.raise_error()

    def prepare(self, query: str, *, name: str | None = None) -> PreparedStatement:
        """
        Parse ``query`` into a named prepared statement and return a
        ``PreparedStatement`` to run it with. This is the libpq equivalent of
        psql's ``<query> \\parse <name>``.

        Pass ``name=`` only when the test cares about the statement's name
        (e.g. it appears in a log line or pg_prepared_statements); otherwise a
        connection-unique ``__p{n}`` name is generated.
        """
        self._check_pending()
        if name is None:
            self._prepared_seq += 1
            name = f"__p{self._prepared_seq}"
        res = self._lib.PQprepare(self._handle, name.encode(), query.encode(), 0, None)
        self._result_or_raise(self._stack.enter_context(PGresult(self._lib, res)))
        return PreparedStatement(self, name)

    def _exec_prepared_impl(self, name: str, *params: Any) -> Any:
        """The PQexecPrepared call behind PreparedStatement.exec()/
        background_exec(); like _sql_impl it runs without the pending guard so
        the worker thread can use it."""
        nparams, values = _build_params(params)
        res = self._lib.PQexecPrepared(
            self._handle, name.encode(), nparams, values, None, None, 0
        )
        return self._result_or_raise(
            self._stack.enter_context(PGresult(self._lib, res))
        )


class PreparedStatement(contextlib.AbstractContextManager):
    """A named server-side prepared statement, created by ``PGconn.prepare()``.

    Execute it with ``exec(*params)`` (or ``background_exec(*params)`` when
    the execution is expected to block), and release it with ``close()`` — or
    use it as a context manager to do that automatically. The statement lives
    on the connection that prepared it, so all methods are subject to that
    connection's single-pending-query rule.
    """

    def __init__(self, conn: PGconn, name: str):
        self._conn = conn
        self.name = name
        self._closed = False

    def exec(self, *params: Any) -> Any:
        """Bind ``params`` to the statement and execute it, returning
        simplified results like ``PGconn.sql()``. This is the libpq equivalent
        of psql's ``\\bind_named <name> <params> \\g``."""
        self._conn._check_pending()
        return self._conn._exec_prepared_impl(self.name, *params)

    def background_exec(self, *params: Any) -> Future[Any]:
        """Execute the statement on the connection's worker thread and return
        a Future, with the same semantics and must-consume rule as
        ``PGconn.background_sql()``."""
        return self._conn._dispatch_background(
            lambda: self._conn._exec_prepared_impl(self.name, *params)
        )

    def close(self) -> None:
        """Release the prepared statement (the protocol-level Close message,
        like DEALLOCATE). Idempotent, and a no-op if the connection itself is
        already gone — the statement died with its session."""
        if self._closed:
            return
        self._closed = True
        if self._conn._handle is None:
            return
        self._conn._check_pending()
        res = self._conn._lib.PQclosePrepared(self._conn._handle, self.name.encode())
        self._conn._result_or_raise(
            self._conn._stack.enter_context(PGresult(self._conn._lib, res))
        )

    def __exit__(self, *exc: object) -> None:
        # Mirror PGconn.__exit__: when an exception is already propagating,
        # don't let close()'s own errors (e.g. an unconsumed background future
        # on the connection) mask it.
        if exc[0] is not None:
            self._closed = True
            return
        self.close()


def connstr(opts: dict[str, Any]) -> str:
    """
    Flattens the provided options into a libpq connection string. Values
    are converted to str and quoted/escaped as necessary.
    """
    settings: list[str] = []

    for k, v in opts.items():
        v = str(v)
        if not v:
            v = "''"
        else:
            v = v.replace("\\", "\\\\")
            v = v.replace("'", "\\'")

            # libpq ends an unquoted value at the first whitespace of any kind
            # (not just a space), so wrap in single quotes whenever the value
            # contains any whitespace.
            if any(c.isspace() for c in v):
                v = f"'{v}'"

        settings.append(f"{k}={v}")

    return " ".join(settings)


def connect(
    libpq_handle: ctypes.CDLL,
    stack: contextlib.ExitStack,
    **opts: Any,
) -> PGconn:
    """
    Connects to a server, using the given connection options, and
    returns a PGconn object wrapping the connection handle. A
    failure will raise LibpqError.

    Args:
        libpq_handle: ctypes.CDLL handle to libpq library
        stack: ExitStack for managing connection cleanup
        **opts: Connection options (host, port, dbname, connect_timeout, etc.)

    Returns:
        PGconn: Connected database connection

    Raises:
        LibpqError: If connection fails
    """

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
