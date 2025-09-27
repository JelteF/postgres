# Copyright (c) 2025, PostgreSQL Global Development Group

import contextlib
import os
import socket
import struct
import threading
from typing import Callable

import pytest


@pytest.mark.parametrize(
    "opts, expected",
    [
        (dict(), ""),
        (dict(port=5432), "port=5432"),
        (dict(port=5432, dbname="postgres"), "port=5432 dbname=postgres"),
        (dict(host=""), "host=''"),
        (dict(host=" "), r"host=' '"),
        (dict(keyword="'"), r"keyword=\'"),
        (dict(keyword=" \\' "), r"keyword=' \\\' '"),
    ],
)
def test_connstr(libpq, opts, expected):
    """Tests the escape behavior for libpq._connstr()."""
    assert libpq._connstr(opts) == expected


def test_must_connect_errors(libpq):
    """Tests that must_connect() raises libpq.Error."""
    with pytest.raises(libpq.Error, match="invalid connection option"):
        libpq.must_connect(some_unknown_keyword="whatever")


@pytest.fixture
def local_server(tmp_path, remaining_timeout):
    """
    Opens up a local UNIX socket for mocking a Postgres server on a background
    thread. See the _Server API for usage.

    This fixture requires AF_UNIX support; dependent tests will be skipped on
    platforms that don't provide it.
    """

    try:
        from socket import AF_UNIX
    except ImportError:
        pytest.skip("AF_UNIX not supported on this platform")

    class _Server(contextlib.ExitStack):
        """
        Implementation class for local_server. See .background() for the primary
        entry point for tests. Postgres clients may connect to this server via
        local_server.host/local_server.port.

        _Server derives from contextlib.ExitStack to provide easy cleanup of
        associated resources; see the documentation for that class for a full
        explanation.
        """

        def __init__(self):
            super().__init__()

            self.host = tmp_path
            self.port = 5432

            self._thread = None
            self._thread_exc = None
            self._listener = self.enter_context(
                socket.socket(AF_UNIX, socket.SOCK_STREAM),
            )

        def bind_and_listen(self):
            """
            Does the actual work of binding the UNIX socket using the Postgres
            server conventions and listening for connections.

            The listen backlog is currently hardcoded to one.
            """
            sockfile = self.host / ".s.PGSQL.{}".format(self.port)

            # Lock down the permissions on the new socket.
            prev_mask = os.umask(0o077)

            # Bind (creating the socket file), and immediately register it for
            # deletion from disk when the stack is cleaned up.
            self._listener.bind(bytes(sockfile))
            self.callback(os.unlink, sockfile)

            os.umask(prev_mask)

            self._listener.listen(1)

        def background(self, fn: Callable[[socket.socket], None]) -> None:
            """
            Accepts a client connection on a background thread and passes it to
            the provided callback. Any exceptions raised from the callback will
            be re-raised on the main thread during fixture teardown.

            Blocking operations on the connected socket default to using the
            remaining_timeout(), though this can be changed by the test via the
            socket's .settimeout().
            """

            def _bg():
                try:
                    self._listener.settimeout(remaining_timeout())
                    sock, _ = self._listener.accept()

                    with sock:
                        sock.settimeout(remaining_timeout())
                        fn(sock)

                except Exception as e:
                    # Save the exception for re-raising on the main thread.
                    self._thread_exc = e

            # TODO: rather than using callback(), consider explicitly signaling
            # the fn() implementation to stop early if we get an exception.
            # Otherwise we'll hang until the end of the timeout.
            self._thread = threading.Thread(target=_bg)
            self.callback(self._join)

            self._thread.start()

        def _join(self):
            """
            Waits for the background thread to finish and raises any thrown
            exception. This is called during fixture teardown.
            """
            # Give a little bit of wiggle room on the join timeout, since we're
            # racing against the test's own use of remaining_timeout(). (It's
            # preferable to let tests report timeouts; the stack traces will
            # help with debugging.)
            self._thread.join(remaining_timeout() + 1)
            if self._thread.is_alive():
                raise TimeoutError("background thread is still running after timeout")

            if self._thread_exc is not None:
                raise self._thread_exc

    with _Server() as s:
        s.bind_and_listen()
        yield s


def test_connection_is_finished_on_error(libpq, local_server, remaining_timeout):
    """Tests that PQfinish() gets called at the end of testing."""
    expected_error = "something is wrong"

    def serve_error(s: socket.socket) -> None:
        pktlen = struct.unpack("!I", s.recv(4))[0]

        # Quick check for the startup packet version.
        version = struct.unpack("!HH", s.recv(4))
        assert version == (3, 0)

        # Discard the remainder of the startup packet and send a v2 error.
        s.recv(pktlen - 8)
        s.send(b"E" + expected_error.encode() + b"\0")

        # And now the socket should be closed.
        assert not s.recv(1), "client sent unexpected data"

    local_server.background(serve_error)

    with pytest.raises(libpq.Error, match=expected_error):
        # Exiting this context should result in PQfinish().
        with libpq:
            libpq.must_connect(host=local_server.host, port=local_server.port)
