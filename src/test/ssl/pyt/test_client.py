# Copyright (c) 2025, PostgreSQL Global Development Group

import contextlib
import ctypes
import socket
import ssl
import struct
import threading
from typing import Callable

import pytest

import pg

# This suite opens up local TCP ports and is hidden behind PG_TEST_EXTRA=ssl.
pytestmark = pg.require_test_extra("ssl")


@pytest.fixture(scope="session", autouse=True)
def skip_if_no_ssl_support(libpq_handle):
    """Skips tests if SSL support is not configured."""

    # Declare PQsslAttribute().
    PQsslAttribute = libpq_handle.PQsslAttribute
    PQsslAttribute.restype = ctypes.c_char_p
    PQsslAttribute.argtypes = [ctypes.c_void_p, ctypes.c_char_p]

    if not PQsslAttribute(None, b"library"):
        pytest.skip("requires SSL support to be configured")


#
# Test Fixtures
#


@pytest.fixture
def tcp_server_class(remaining_timeout):
    """
    Metafixture to combine related logic for tcp_server and ssl_server.

    TODO: combine with test_libpq.local_server
    """

    class _TCPServer(contextlib.ExitStack):
        """
        Implementation class for tcp_server. See .background() for the primary
        entry point for tests. Postgres clients may connect to this server via
        **tcp_server.conninfo.

        _TCPServer derives from contextlib.ExitStack to provide easy cleanup of
        associated resources; see the documentation for that class for a full
        explanation.
        """

        def __init__(self):
            super().__init__()

            self._thread = None
            self._thread_exc = None
            self._listener = self.enter_context(
                socket.socket(socket.AF_INET, socket.SOCK_STREAM),
            )

            self._bind_and_listen()
            sockname = self._listener.getsockname()
            self.conninfo = dict(
                hostaddr=sockname[0],
                port=sockname[1],
            )

        def _bind_and_listen(self):
            """
            Does the actual work of binding the socket and listening for
            connections.

            The listen backlog is currently hardcoded to one.
            """
            self._listener.bind(("127.0.0.1", 0))
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

    return _TCPServer


@pytest.fixture
def tcp_server(tcp_server_class):
    """
    Opens up a local TCP socket for mocking a Postgres server on a background
    thread. See the _TCPServer API for usage.
    """
    with tcp_server_class() as s:
        yield s


@pytest.fixture
def ssl_server(tcp_server_class, certs):
    """
    Like tcp_server, but with an additional .background_ssl() method which will
    perform a SSLRequest handshake on the socket before handing the connection
    to the test callback.

    This server uses certs.server as its identity.
    """

    class _SSLServer(tcp_server_class):
        def __init__(self):
            super().__init__()

            self.conninfo["host"] = certs.server_host

            self._ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self._ctx.load_cert_chain(certs.server.certpath, certs.server.keypath)

        def background_ssl(self, fn: Callable[[ssl.SSLSocket], None]) -> None:
            """
            Invokes a server callback as with .background(), but an SSLRequest
            handshake is performed first, and the socket provided to the
            callback has been wrapped in an OpenSSL layer.
            """

            def handshake(s: socket.socket):
                pktlen = struct.unpack("!I", s.recv(4))[0]

                # Make sure we get an SSLRequest.
                version = struct.unpack("!HH", s.recv(4))
                assert version == (1234, 5679)
                assert pktlen == 8

                # Accept the SSLRequest.
                s.send(b"S")

                with self._ctx.wrap_socket(s, server_side=True) as wrapped:
                    fn(wrapped)

            self.background(handshake)

    with _SSLServer() as s:
        yield s


#
# Tests
#


@pytest.mark.parametrize("sslmode", ("require", "verify-ca", "verify-full"))
def test_server_with_ssl_disabled(libpq, tcp_server, certs, sslmode):
    """
    Make sure client refuses to talk to non-SSL servers with stricter
    sslmodes.
    """

    def refuse_ssl(s: socket.socket):
        pktlen = struct.unpack("!I", s.recv(4))[0]

        # Make sure we get an SSLRequest.
        version = struct.unpack("!HH", s.recv(4))
        assert version == (1234, 5679)
        assert pktlen == 8

        # Refuse the SSLRequest.
        s.send(b"N")

        # Wait for the client to close the connection.
        assert not s.recv(1), "client sent unexpected data"

    tcp_server.background(refuse_ssl)

    with pytest.raises(libpq.Error, match="server does not support SSL"):
        with libpq:  # XXX tests shouldn't need to do this
            libpq.must_connect(
                **tcp_server.conninfo,
                sslrootcert=certs.ca.certpath,
                sslmode=sslmode,
            )


def test_verify_full_connection(libpq, ssl_server, certs):
    """Completes a verify-full connection and empty query."""

    def handle_empty_query(s: ssl.SSLSocket):
        pktlen = struct.unpack("!I", s.recv(4))[0]

        # Check the startup packet version, then discard the remainder.
        version = struct.unpack("!HH", s.recv(4))
        assert version == (3, 0)
        s.recv(pktlen - 8)

        # Send the required litany of server messages.
        s.send(struct.pack("!cII", b"R", 8, 0))  # AuthenticationOK

        # ParameterStatus: client_encoding
        key = b"client_encoding\0"
        val = b"UTF-8\0"
        s.send(struct.pack("!cI", b"S", 4 + len(key) + len(val)) + key + val)

        # ParameterStatus: DateStyle
        key = b"DateStyle\0"
        val = b"ISO, MDY\0"
        s.send(struct.pack("!cI", b"S", 4 + len(key) + len(val)) + key + val)

        s.send(struct.pack("!cIII", b"K", 12, 1234, 1234))  # BackendKeyData
        s.send(struct.pack("!cIc", b"Z", 5, b"I"))  # ReadyForQuery

        # Expect an empty query.
        pkttype = s.recv(1)
        assert pkttype == b"Q"
        pktlen = struct.unpack("!I", s.recv(4))[0]
        assert s.recv(pktlen - 4) == b"\0"

        # Send an EmptyQueryResponse+ReadyForQuery.
        s.send(struct.pack("!cI", b"I", 4))
        s.send(struct.pack("!cIc", b"Z", 5, b"I"))

        # libpq should terminate and close the connection.
        assert s.recv(1) == b"X"
        pktlen = struct.unpack("!I", s.recv(4))[0]
        assert pktlen == 4

        assert not s.recv(1), "client sent unexpected data"

    ssl_server.background_ssl(handle_empty_query)

    conn = libpq.must_connect(
        **ssl_server.conninfo,
        sslrootcert=certs.ca.certpath,
        sslmode="verify-full",
    )
    with conn:
        assert conn.exec("").status() == libpq.PGRES_EMPTY_QUERY
