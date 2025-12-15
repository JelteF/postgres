# Copyright (c) 2025, PostgreSQL Global Development Group

import os
import contextlib
import pathlib
import secrets
import time

import pytest

from ._env import test_timeout_default
from .util import capture
from .server import PostgresServer

from libpq import load_libpq_handle, connect as libpq_connect


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


@pytest.fixture(scope="session")
def libpq_handle(libdir):
    """
    Loads a ctypes handle for libpq. Some common function prototypes are
    initialized for general use.
    """
    try:
        return load_libpq_handle(libdir)
    except OSError as e:
        if "wrong ELF class" in str(e):
            # This happens in CI when trying to lead a 32-bit libpq library
            # with a 64-bit Python
            pytest.skip("libpq architecture does not match Python interpreter")
        raise


@pytest.fixture
def connect(libpq_handle, remaining_timeout):
    """
    Returns a function to connect to PostgreSQL via libpq.

    The returned function accepts connection options as keyword arguments
    (host, port, dbname, etc.) and returns a PGconn object. Connections
    are automatically cleaned up at the end of the test.

    Example:
        conn = connect(host='localhost', port=5432, dbname='postgres')
        result = conn.sql("SELECT 1")
    """
    with contextlib.ExitStack() as stack:

        def _connect(**opts):
            return libpq_connect(libpq_handle, stack, remaining_timeout, **opts)

        yield _connect


@pytest.fixture(scope="session")
def pg_config():
    """
    Returns the path to pg_config. Uses PG_CONFIG environment variable if set,
    otherwise uses 'pg_config' from PATH.
    """
    return os.environ.get("PG_CONFIG", "pg_config")


@pytest.fixture(scope="session")
def bindir(pg_config):
    """
    Returns the PostgreSQL bin directory using pg_config --bindir.
    """
    return capture(pg_config, "--bindir")


@pytest.fixture(scope="session")
def libdir(pg_config):
    """
    Returns the PostgreSQL lib directory using pg_config --libdir.
    """
    return capture(pg_config, "--libdir")


@pytest.fixture(scope="session")
def datadir(tmp_path_factory):
    """
    Returns the directory name to use as the server data directory. If
    TESTDATADIR is provided, that will be used; otherwise a new temporary
    directory is created in the pytest temp root.
    """
    d = os.getenv("TESTDATADIR")
    if d:
        d = pathlib.Path(d)
    else:
        d = tmp_path_factory.mktemp("tmp_check")

    return d


@pytest.fixture(scope="session")
def sockdir(tmp_path_factory):
    """
    Returns the directory name to use as the server's unix_socket_directories
    setting. Local client connections use this as the PGHOST.

    At the moment, this is always put under the pytest temp root.
    """
    return tmp_path_factory.mktemp("sockfiles")


@pytest.fixture(scope="session")
def winpassword():
    """The per-session SCRAM password for the server admin on Windows."""
    return secrets.token_urlsafe(16)


@pytest.fixture(scope="session")
def pg_server_global(bindir, datadir, sockdir, winpassword, libpq_handle):
    """
    Starts a running Postgres server listening on localhost. The HBA initially
    allows only local UNIX connections from the same user.

    Returns a PostgresServer instance with methods for server management, configuration,
    and creating test databases/users.
    """
    server = PostgresServer(bindir, datadir, sockdir, winpassword, libpq_handle)

    yield server

    # Cleanup any test resources
    server.cleanup()

    # Stop the server
    server.stop()


@pytest.fixture(scope="module")
def pg_server_module(pg_server_global):
    """
    Module-scoped server context. Which can be useful so that certain settings
    can be overriden at the module level through autouse fixtures. An example
    of this is in the SSL tests.
    """
    with pg_server_global.subcontext() as s:
        yield s


@pytest.fixture
def pg(pg_server_module, remaining_timeout):
    """
    Per-test server context. Use this fixture to make changes to the server
    which will be rolled back at the end of the test (e.g., creating test
    users/databases).
    """
    pg_server_module.set_timeout(remaining_timeout)
    with pg_server_module.subcontext() as s:
        yield s


@pytest.fixture
def conn(pg):
    """
    Returns a connected PGconn instance to the test PostgreSQL server.
    The connection is automatically cleaned up at the end of the test.

    Example:
        def test_something(conn):
            result = conn.sql("SELECT 1")
            assert result == 1
    """
    return pg.connect()
