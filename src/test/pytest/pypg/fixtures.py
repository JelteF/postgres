# Copyright (c) 2025, PostgreSQL Global Development Group

import os
import contextlib
import pathlib
import tempfile
import time
from typing import List

import pytest

from ._env import test_timeout_default
from .server import PostgresServer

from libpq import load_libpq_handle, connect as libpq_connect


# Stash key for tracking servers for log reporting.
_servers_key = pytest.StashKey[List[PostgresServer]]()


def _record_server_for_log_reporting(request, server):
    """Record a server for log reporting on test failure."""
    if _servers_key not in request.node.stash:
        request.node.stash[_servers_key] = []
    request.node.stash[_servers_key].append(server)


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


@pytest.fixture(scope="module")
def remaining_timeout_module():
    """
    Same as remaining_timeout, but the deadline is set once per module.

    This fixture is per-module, which means it's generally only really useful
    for configuring timeouts of operations that happen in the setup phase of
    another module fixtures. If you use it in a test it would mean that each
    subsequent test in the module gets a reduced timeout.
    """
    now = time.monotonic()
    deadline = now + test_timeout_default()

    return lambda: max(deadline - time.monotonic(), 0)


@pytest.fixture(scope="session")
def libpq_handle():
    """
    Loads a ctypes handle for libpq. Some common function prototypes are
    initialized for general use.
    """
    try:
        return load_libpq_handle()
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
def tmp_check(tmp_path_factory) -> pathlib.Path:
    """
    Returns the tmp_check directory that should be used for the tests. If
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
def datadir(tmp_check):
    """
    Returns the data directory to use for the pg fixture.
    """

    return tmp_check / "pgdata"


@pytest.fixture(scope="session")
def sockdir():
    """
    Returns the directory name to use as the server's unix_socket_directories
    setting. Local client connections use this as the PGHOST.

    Uses tempfile.TemporaryDirectory directly instead of pytest's
    tmp_path_factory, because macOS limits Unix socket paths to 104 bytes
    and pytest's nested temp directories can exceed that.
    """
    with tempfile.TemporaryDirectory(prefix="pytest_postgres_sock") as d:
        yield pathlib.Path(d)


@pytest.fixture(scope="session")
def pg_server_global(request, datadir, sockdir, libpq_handle):
    """
    Starts a running Postgres server listening on localhost. The HBA initially
    allows only local UNIX connections from the same user.

    Returns a PostgresServer instance with methods for server management, configuration,
    and creating test databases/users.
    """
    server = PostgresServer("default", datadir, sockdir, libpq_handle)
    try:
        server.start()
    except Exception:
        # normally we only add the global server for reporting when the test
        # actually uses the pg fixture. But if the server fails to start here,
        # then we won't have that opportunity, so add it now to ensure any
        # startup logs are included in the report.
        _record_server_for_log_reporting(request, server)
        raise

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
def pg(request, pg_server_module, remaining_timeout):
    """
    Per-test server context. Use this fixture to make changes to the server
    which will be rolled back at the end of the test (e.g., creating test
    users/databases).

    Also captures the PostgreSQL log position at test start so that any new
    log entries can be included in the test report on failure.
    """
    with pg_server_module.start_new_test(remaining_timeout) as s:
        _record_server_for_log_reporting(request, s)
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


@pytest.fixture
def create_pg(request, sockdir, libpq_handle, tmp_check, remaining_timeout):
    """
    Factory fixture to create additional PostgreSQL servers (per-test scope).

    Returns a function that creates new PostgreSQL server instances.
    Servers are automatically cleaned up at the end of the test.

    Example:
        def test_multiple_servers(create_pg):
            node1 = create_pg()
            node2 = create_pg()
            node3 = create_pg()
    """
    servers = []

    def _create(name=None, start=True, **kwargs):
        if name is None:
            count = len(servers) + 1
            name = f"pg{count}"

        datadir = tmp_check / f"pgdata_{name}"
        server = PostgresServer(name, datadir, sockdir, libpq_handle, **kwargs)
        servers.append(server)
        _record_server_for_log_reporting(request, server)
        server.set_timeout(remaining_timeout)
        # Pass start=False when the test must touch the data directory before
        # startup (e.g. drop an extra signal file) or expects startup to fail;
        # call server.start() yourself afterwards.
        if start:
            server.start()
        return server

    yield _create

    for server in servers:
        server.cleanup()
        server.stop()


@pytest.fixture(scope="module")
def _module_scoped_servers():
    """Session-scoped list to track servers created by create_pg_module."""
    return []


@pytest.fixture(scope="module")
def create_pg_module(
    request,
    sockdir,
    libpq_handle,
    tmp_check,
    remaining_timeout_module,
    _module_scoped_servers,
):
    """
    Factory fixture to create additional PostgreSQL servers (module scope).

    Like create_pg, but servers persist for the entire test module.
    Use this when multiple tests in a module can share the same servers.

    The timeout is automatically set on all servers at the start of each test
    via the _set_module_server_timeouts autouse fixture.

    Example:
        @pytest.fixture(scope="module")
        def shared_nodes(create_pg_module):
            return [create_pg_module() for _ in range(3)]
    """

    def _create(name=None, start=True, **kwargs):
        if name is None:
            count = len(_module_scoped_servers) + 1
            name = f"pg{count}"
        datadir = tmp_check / f"pgdata_{name}"
        server = PostgresServer(name, datadir, sockdir, libpq_handle, **kwargs)
        _module_scoped_servers.append(server)
        _record_server_for_log_reporting(request, server)
        server.set_timeout(remaining_timeout_module)
        if start:
            server.start()
        return server

    yield _create

    for server in _module_scoped_servers:
        server.cleanup()
        server.stop()


@pytest.fixture(autouse=True)
def _set_module_server_timeouts(_module_scoped_servers, remaining_timeout):
    """Registers all module-scoped servers for this test.

    It's hard to reliably detect whether a test uses a module-scoped server or
    not. So this simply assumes all tests in the module use the module-scoped
    servers. There's little harm in registering servers for tests that don't
    use them.
    """
    with contextlib.ExitStack() as stack:
        for server in _module_scoped_servers:
            stack.enter_context(server.start_new_test(remaining_timeout))
        yield


@pytest.hookimpl(hookwrapper=True, trylast=True)
def pytest_runtest_makereport(item, call):
    """
    Adds PostgreSQL server logs to the test report sections.
    """
    outcome = yield
    report = outcome.get_result()

    session_servers = item.session.stash.get(_servers_key, [])

    module_node = item.getparent(pytest.Module)
    module_servers = module_node.stash.get(_servers_key, []) if module_node else []

    servers = session_servers + module_servers + item.stash.get(_servers_key, [])

    include_name = len(servers) > 1

    for server in servers:
        content = server.log_content()
        if content.strip():
            section_title = f"Postgres log {report.when}"
            if include_name:
                section_title += f" ({server.name})"
            report.sections.append((section_title, content))
        server.reset_log_position()
