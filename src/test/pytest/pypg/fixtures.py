# Copyright (c) 2025, PostgreSQL Global Development Group

from __future__ import annotations

import os
import contextlib
import ctypes
import pathlib
import tempfile
from collections.abc import Callable, Iterator
from typing import Any, List

import pytest

from ._env import test_timeout_default
from .paths import bindir, libdir
from .server import PostgresServer

from libpq import PGconn, load_libpq_handle, connect as libpq_connect


# Stash key for tracking servers for log reporting.
_servers_key = pytest.StashKey[List[PostgresServer]]()


def _record_server_for_log_reporting(
    request: pytest.FixtureRequest, server: PostgresServer
) -> None:
    """Record a server for log reporting on test failure."""
    if _servers_key not in request.node.stash:
        request.node.stash[_servers_key] = []
    request.node.stash[_servers_key].append(server)


@pytest.fixture(scope="session")
def libpq_handle() -> ctypes.CDLL:
    """
    Loads a ctypes handle for libpq. Some common function prototypes are
    initialized for general use.

    Session-scoped because the loaded library is immutable, process-global
    state: there is nothing per-module to isolate, so there is no reason to
    reload it for every module when several run in one process.
    """
    try:
        return load_libpq_handle(bindir(), libdir())
    except OSError as e:
        if "wrong ELF class" in str(e):
            # This happens in CI when trying to lead a 32-bit libpq library
            # with a 64-bit Python
            pytest.skip("libpq architecture does not match Python interpreter")
        raise


@pytest.fixture
def connect(libpq_handle: ctypes.CDLL) -> Iterator[Callable[..., PGconn]]:
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

        def _connect(**opts: object) -> PGconn:
            opts.setdefault("connect_timeout", test_timeout_default())
            return libpq_connect(libpq_handle, stack, **opts)

        yield _connect


@pytest.fixture(scope="module")
def tmp_check(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """
    Returns the tmp_check directory that should be used for the tests. If
    TESTDATADIR is provided, that will be used; otherwise a new temporary
    directory is created in the pytest temp root.

    Module-scoped, so that when several test files are run in a single pytest
    process (e.g. ``pytest pyt/`` locally, without TESTDATADIR set) each module
    gets its own temp root and the per-module servers below never collide on a
    data directory. Under meson TESTDATADIR is set and there is one module per
    process, so this returns the same path either way.
    """
    d = os.getenv("TESTDATADIR")
    if d:
        d = pathlib.Path(d)
    else:
        d = tmp_path_factory.mktemp("tmp_check")

    return d


@pytest.fixture(scope="module")
def datadir(tmp_check: pathlib.Path) -> pathlib.Path:
    """
    Returns the data directory to use for the pg fixture. Unique per module via
    the module-scoped tmp_check.
    """

    return tmp_check / "pgdata"


@pytest.fixture(scope="module")
def sockdir() -> Iterator[pathlib.Path]:
    """
    Returns the directory name to use as the server's unix_socket_directories
    setting. Local client connections use this as the PGHOST.

    Uses tempfile.TemporaryDirectory directly instead of pytest's
    tmp_path_factory, because macOS limits Unix socket paths to 104 bytes
    and pytest's nested temp directories can exceed that.
    """
    with tempfile.TemporaryDirectory(prefix="pytest_postgres_sock") as d:
        yield pathlib.Path(d)


@pytest.fixture(scope="module")
def pg_server_module(
    request: pytest.FixtureRequest,
    datadir: pathlib.Path,
    sockdir: pathlib.Path,
    libpq_handle: ctypes.CDLL,
) -> Iterator[PostgresServer]:
    """
    Starts a running Postgres server for the test module, listening on
    localhost. The HBA initially allows only local UNIX connections from the
    same user.

    This is module-scoped rather than session-scoped on purpose. Meson runs
    every test file in its own process (each ``.py`` is a separate ``test()``
    target, see meson.build), so a session never spans more than one module.
    We would get different behavior if we made this session-scoped and ran
    multiple modules in the same pytest process (e.g. ``pytest pyt/`` locally)

    Per-test isolation is a separate concern, handled by the ``pg`` fixture,
    which opens a per-test cleanup subcontext via ``start_new_test()``.

    Returns a PostgresServer instance with methods for server management,
    configuration, and creating test databases/users.
    """
    server = PostgresServer("default", datadir, sockdir, libpq_handle)
    try:
        server.start()
    except Exception:
        # If startup fails the tests never run, so they never get the chance to
        # register the server for log reporting; do it here so the startup logs
        # still make it into the failure report.
        _record_server_for_log_reporting(request, server)
        raise

    yield server

    # Cleanup any test resources, then stop the server.
    server.cleanup()
    server.stop()


@pytest.fixture
def pg(
    request: pytest.FixtureRequest, pg_server_module: PostgresServer
) -> Iterator[PostgresServer]:
    """
    Per-test server context. Use this fixture to make changes to the server
    which will be rolled back at the end of the test (e.g., creating test
    users/databases).

    Also captures the PostgreSQL log position at test start so that any new
    log entries can be included in the test report on failure.
    """
    with pg_server_module.start_new_test() as s:
        _record_server_for_log_reporting(request, s)
        yield s


@pytest.fixture
def conn(pg: PostgresServer) -> PGconn:
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
def create_pg(
    request: pytest.FixtureRequest,
    sockdir: pathlib.Path,
    libpq_handle: ctypes.CDLL,
    tmp_check: pathlib.Path,
) -> Iterator[Callable[..., PostgresServer]]:
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
    servers: list[PostgresServer] = []

    def _create(
        name: str | None = None, start: bool = True, **kwargs: Any
    ) -> PostgresServer:
        if name is None:
            count = len(servers) + 1
            name = f"pg{count}"

        datadir = tmp_check / f"pgdata_{name}"
        server = PostgresServer(name, datadir, sockdir, libpq_handle, **kwargs)
        servers.append(server)
        _record_server_for_log_reporting(request, server)
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
def _module_scoped_servers() -> list[PostgresServer]:
    """Session-scoped list to track servers created by create_pg_module."""
    return []


@pytest.fixture(scope="module")
def create_pg_module(
    request: pytest.FixtureRequest,
    sockdir: pathlib.Path,
    libpq_handle: ctypes.CDLL,
    tmp_check: pathlib.Path,
    _module_scoped_servers: list[PostgresServer],
) -> Iterator[Callable[..., PostgresServer]]:
    """
    Factory fixture to create additional PostgreSQL servers (module scope).

    Like create_pg, but servers persist for the entire test module.
    Use this when multiple tests in a module can share the same servers.

    A new per-test subcontext is opened on all servers at the start of each
    test via the _start_module_server_tests autouse fixture.

    Example:
        @pytest.fixture(scope="module")
        def shared_nodes(create_pg_module):
            return [create_pg_module() for _ in range(3)]
    """

    def _create(
        name: str | None = None, start: bool = True, **kwargs: Any
    ) -> PostgresServer:
        if name is None:
            count = len(_module_scoped_servers) + 1
            name = f"pg{count}"
        datadir = tmp_check / f"pgdata_{name}"
        server = PostgresServer(name, datadir, sockdir, libpq_handle, **kwargs)
        _module_scoped_servers.append(server)
        _record_server_for_log_reporting(request, server)
        if start:
            server.start()
        return server

    yield _create

    for server in _module_scoped_servers:
        server.cleanup()
        server.stop()


@pytest.fixture(autouse=True)
def _start_module_server_tests(
    _module_scoped_servers: list[PostgresServer],
) -> Iterator[None]:
    """Opens a per-test subcontext on all module-scoped servers for this test.

    It's hard to reliably detect whether a test uses a module-scoped server or
    not. So this simply assumes all tests in the module use the module-scoped
    servers. There's little harm in registering servers for tests that don't
    use them.
    """
    with contextlib.ExitStack() as stack:
        for server in _module_scoped_servers:
            stack.enter_context(server.start_new_test())
        yield


@pytest.hookimpl(hookwrapper=True, trylast=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]):
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
