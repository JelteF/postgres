# Copyright (c) 2025, PostgreSQL Global Development Group

import contextlib
import os
import pathlib
import platform
import re
import shutil
import socket
import subprocess
import tempfile
from collections import namedtuple
from typing import Callable, Optional

from .util import run, wait_until
from libpq import PGconn, connect as libpq_connect


class FileBackup(contextlib.AbstractContextManager):
    """
    A context manager which backs up a file's contents, restoring them on exit.
    """

    def __init__(self, file: pathlib.Path):
        super().__init__()

        self._file = file

    def __enter__(self):
        with tempfile.NamedTemporaryFile(
            prefix=self._file.name, dir=self._file.parent, delete=False
        ) as f:
            self._backup = pathlib.Path(f.name)

        shutil.copyfile(self._file, self._backup)

        return self

    def __exit__(self, *exc):
        # Swap the backup and the original file, so that the modified contents
        # can still be inspected in case of failure.
        tmp = self._backup.parent / (self._backup.name + ".tmp")

        shutil.copyfile(self._file, tmp)
        shutil.copyfile(self._backup, self._file)
        shutil.move(tmp, self._backup)


class HBA(FileBackup):
    """
    Backs up a server's HBA configuration and provides means for temporarily
    editing it.
    """

    def __init__(self, datadir: pathlib.Path):
        super().__init__(datadir / "pg_hba.conf")

    def prepend(self, *lines):
        """
        Temporarily prepends lines to the server's pg_hba.conf.

        As sugar for aligning HBA columns in the tests, each line can be either
        a string or a list of strings. List elements will be joined by single
        spaces before they are written to file.
        """
        with open(self._file, "r") as f:
            prior_data = f.read()

        with open(self._file, "w") as f:
            for line in lines:
                if isinstance(line, list):
                    print(*line, file=f)
                else:
                    print(line, file=f)

            f.write(prior_data)


class Config(FileBackup):
    """
    Backs up a server's postgresql.conf and provides means for temporarily
    editing it.
    """

    def __init__(self, datadir: pathlib.Path):
        super().__init__(datadir / "postgresql.conf")

    def set(self, **gucs):
        """
        Temporarily appends GUC settings to the server's postgresql.conf.
        """

        with open(self._file, "a") as f:
            print(file=f)

            for n, v in gucs.items():
                v = str(v)

                # Quote and escape the value for postgresql.conf single-quoted
                # strings. This is doing the reversee of DeescapeQuotedString.
                v = v.replace("\\", "\\\\")
                v = v.replace("'", "''")
                v = v.replace("\n", "\\n")
                v = v.replace("\r", "\\r")
                v = v.replace("\t", "\\t")
                v = v.replace("\b", "\\b")
                v = v.replace("\f", "\\f")
                v = "'{}'".format(v)

                print(n, "=", v, file=f)


Backup = namedtuple("Backup", "conf, hba")


class PostgresServer:
    """
    Represents a running PostgreSQL server instance with management utilities.
    Provides methods for configuration, user/database creation, and server control.
    """

    def __init__(
        self,
        name,
        bindir,
        datadir,
        sockdir,
        libpq_handle,
        *,
        hostaddr: Optional[str] = None,
        port: Optional[int] = None,
    ):
        """
        Initialize a PostgreSQL server instance. Call start() to actually
        start the server.

        Args:
            name: The name of this server instance (for logging purposes)
            bindir: Path to PostgreSQL bin directory
            datadir: Path to data directory for this server
            sockdir: Path to directory for Unix sockets
            libpq_handle: ctypes handle to libpq
            hostaddr: If provided, use this specific address (e.g., "127.0.0.2")
            port: If provided, use this port instead of finding a free one,
                is currently only allowed if hostaddr is also provided
        """

        if hostaddr is None and port is not None:
            raise NotImplementedError("port was provided without hostaddr")

        self.name = name
        self.datadir = datadir
        self.sockdir = sockdir
        self.libpq_handle = libpq_handle
        self._remaining_timeout_fn: Optional[Callable[[], float]] = None
        self._bindir = bindir
        self._pg_ctl = bindir / "pg_ctl"
        self.log = datadir / "postgresql.log"
        self._log_start_pos = 0

        # ExitStack for cleanup callbacks
        self._cleanup_stack = contextlib.ExitStack()

        # Determine whether to use Unix sockets
        use_unix_sockets = platform.system() != "Windows" and hostaddr is None

        # Use INITDB_TEMPLATE if available (much faster than running initdb)
        initdb_template = os.environ.get("INITDB_TEMPLATE")
        if initdb_template and os.path.isdir(initdb_template):
            shutil.copytree(initdb_template, datadir)
        else:
            if platform.system() == "Windows":
                auth_method = "trust"
            else:
                auth_method = "peer"
            run(
                bindir / "initdb",
                "--no-sync",
                "--auth",
                auth_method,
                "--pgdata",
                self.datadir,
            )

        # Figure out a port to listen on. Attempt to reserve both IPv4 and IPv6
        # addresses in one go.
        #
        # Note: socket.has_dualstack_ipv6/create_server are only in Python 3.8+.
        if hostaddr is not None:
            # Explicit address provided
            addrs: list[str] = [hostaddr]
            temp_sock = socket.socket()
            if port is None:
                temp_sock.bind((hostaddr, 0))
                _, port = temp_sock.getsockname()

        elif hasattr(socket, "has_dualstack_ipv6") and socket.has_dualstack_ipv6():
            addr = ("::1", 0)
            temp_sock = socket.create_server(
                addr, family=socket.AF_INET6, dualstack_ipv6=True
            )

            hostaddr, port, _, _ = temp_sock.getsockname()
            assert hostaddr is not None
            addrs = [hostaddr, "127.0.0.1"]

        else:
            addr = ("127.0.0.1", 0)

            temp_sock = socket.socket()
            temp_sock.bind(addr)

            hostaddr, port = temp_sock.getsockname()
            assert hostaddr is not None
            addrs = [hostaddr]

        # Store the computed values
        self.hostaddr = hostaddr
        self.port = port
        # Including the host to use for connections - either the socket
        # directory or TCP address
        if use_unix_sockets:
            self.host = str(sockdir)
        else:
            self.host = hostaddr

        with open(os.path.join(datadir, "postgresql.conf"), "a") as f:
            print(file=f)
            if use_unix_sockets:
                print(
                    "unix_socket_directories = '{}'".format(sockdir.as_posix()),
                    file=f,
                )
            else:
                # Disable Unix sockets when using TCP to avoid lock conflicts
                print("unix_socket_directories = ''", file=f)
            print("listen_addresses = '{}'".format(",".join(addrs)), file=f)
            print("port =", port, file=f)
            print("log_connections = all", file=f)
            print("fsync = off", file=f)
            print("datestyle = 'ISO'", file=f)
            print("timezone = 'UTC'", file=f)

        # Between closing of the socket, s, and server start, we're racing
        # against anything that wants to open up ephemeral ports, so try not to
        # put any new work here.

        temp_sock.close()

    def start(self):
        """Start the server using pg_ctl."""
        self.pg_ctl("start")

        # Read the PID file to get the postmaster PID
        with open(os.path.join(self.datadir, "postmaster.pid")) as f:
            self.pid = int(f.readline().strip())

    def current_log_position(self):
        """Get the current end position of the log file."""
        if self.log.exists():
            return self.log.stat().st_size
        return 0

    def reset_log_position(self):
        """Mark current log position as start for log_content()."""
        self._log_start_pos = self.current_log_position()

    @contextlib.contextmanager
    def start_new_test(self, remaining_timeout):
        """
        Prepare server for a new test.

        Sets timeout, resets log position, and enters a cleanup subcontext.
        """
        self.set_timeout(remaining_timeout)
        self.reset_log_position()
        with self.subcontext():
            yield self

    def psql(self, *args):
        """Run psql with the given arguments."""
        self._run(os.path.join(self._bindir, "psql"), "-w", *args)

    def sql(self, query):
        """Execute a SQL query via libpq. Returns simplified results."""
        with self.connect() as conn:
            return conn.sql(query)

    def append_conf(self, *lines, filename="postgresql.conf"):
        """Append config lines to a file in the data directory.

        Each positional argument is one config line (without a trailing
        newline). Passing no lines still ensures the file exists, which is handy
        for signal files like ``standby.signal``.

        Unlike reloading()/restarting(), this does not reload the server and is
        not undone automatically; use it for configuration that must be present
        before the server (re)starts.
        """
        with open(self.datadir / filename, "a") as f:
            for line in lines:
                f.write(line + "\n")

    def poll_query_until(self, query, expected=True, dbname="postgres", timeout=None):
        """Run ``query`` repeatedly until it returns ``expected``.

        The comparison is against the simplified Python result of ``sql()`` (so
        ``expected`` is ``True`` for a boolean ``t`` probe, an ``int`` for a
        count, a tuple for a multi-column row, and so on) rather than psql text.
        Returns the matching result, or raises ``TimeoutError`` once the timeout
        (defaulting to the test's remaining timeout) is exhausted.
        """
        if timeout is None:
            timeout = self._remaining_timeout_fn() if self._remaining_timeout_fn else 180
        # Close the polling connection on return rather than leaking it until
        # teardown; a lingering connection to ``dbname`` would otherwise block
        # e.g. CREATE DATABASE WITH TEMPLATE on that database.
        with self.connect(dbname=dbname) as conn:
            for _ in wait_until(
                f"query never returned {expected!r}: {query}", timeout=timeout
            ):
                result = conn.sql(query)
                if result == expected:
                    return result

    def pg_ctl(self, *args):
        """Run pg_ctl with the given arguments."""
        self._run(self._pg_ctl, "--pgdata", self.datadir, "--log", self.log, *args)

    def _run(self, cmd, *args, addenv: Optional[dict] = None):
        """Run a command with PG* environment variables set."""
        subenv = dict(os.environ)
        subenv.update(
            {
                "PGHOST": str(self.host),
                "PGPORT": str(self.port),
                "PGDATABASE": "postgres",
                "PGDATA": str(self.datadir),
            }
        )
        if addenv:
            subenv.update(addenv)
        run(cmd, *args, env=subenv)

    def create_users(self, *userkeys: str):
        """Create test users and register them for cleanup."""
        usermap = {}
        for u in userkeys:
            name = u + "user"
            usermap[u] = name
            self.psql("-c", "CREATE USER " + name)
            self._cleanup_stack.callback(self.psql, "-c", "DROP USER " + name)
        return usermap

    def create_dbs(self, *dbkeys: str):
        """Create test databases and register them for cleanup."""
        dbmap = {}
        for d in dbkeys:
            name = d + "db"
            dbmap[d] = name
            self.psql("-c", "CREATE DATABASE " + name)
            self._cleanup_stack.callback(self.psql, "-c", "DROP DATABASE " + name)
        return dbmap

    @contextlib.contextmanager
    def reloading(self):
        """
        Provides a context manager for making configuration changes.

        If the context suite finishes successfully, the configuration will
        be reloaded via pg_ctl. On teardown, the configuration changes will
        be unwound, and the server will be signaled to reload again.

        The context target contains the following attributes which can be
        used to configure the server:
        - .conf: modifies postgresql.conf
        - .hba: modifies pg_hba.conf

        For example:

            with pg_server_session.reloading() as s:
                s.conf.set(log_connections="on")
                s.hba.prepend("local all all trust")
        """
        # Push a reload onto the stack before making any other
        # unwindable changes. That way the order of operations will be
        #
        #  # test
        #   - config change 1
        #   - config change 2
        #   - reload
        #  # teardown
        #   - undo config change 2
        #   - undo config change 1
        #   - reload
        #
        self._cleanup_stack.callback(self.pg_ctl, "reload")
        yield self._backup_configuration()

        # Now actually reload
        self.pg_ctl("reload")

    @contextlib.contextmanager
    def restarting(self):
        """Like .reloading(), but with a full server restart."""
        self._cleanup_stack.callback(self.pg_ctl, "restart")
        yield self._backup_configuration()
        self.pg_ctl("restart")

    def _backup_configuration(self):
        # Wrap the existing HBA and configuration with FileBackups.
        return Backup(
            hba=self._cleanup_stack.enter_context(HBA(self.datadir)),
            conf=self._cleanup_stack.enter_context(Config(self.datadir)),
        )

    @contextlib.contextmanager
    def subcontext(self):
        """
        Create a new cleanup context for per-test isolation.

        Temporarily replaces the cleanup stack so that any cleanup callbacks
        registered within this context will be cleaned up when the context exits.
        """
        old_stack = self._cleanup_stack
        self._cleanup_stack = contextlib.ExitStack()
        try:
            self._cleanup_stack.__enter__()
            yield self
        finally:
            self._cleanup_stack.__exit__(None, None, None)
            self._cleanup_stack = old_stack

    def stop(self, mode="fast"):
        """
        Stop the PostgreSQL server instance.

        Ignores failures if the server is already stopped.
        """
        try:
            self.pg_ctl("stop", "--mode", mode)
        except subprocess.CalledProcessError:
            # Server may have already been stopped
            pass

    def log_content(self) -> str:
        """Return log content from the current context's start position."""
        return self.log_since(self._log_start_pos)

    def log_since(self, offset: int) -> str:
        """Return log content written since the given byte offset.

        Pair with current_log_position() to capture exactly the log a single
        operation produces::

            offset = pg.current_log_position()
            conn.sql("...")
            assert "..." in pg.log_since(offset)
        """
        if not self.log.exists():
            return ""
        with open(self.log) as f:
            f.seek(offset)
            return f.read()

    def wait_for_log(self, pattern, offset=0, timeout=None):
        """Wait until the log written since ``offset`` matches ``pattern``.

        Returns the log's end offset once the regex matches, so chained waits
        can continue from there. Raises ``TimeoutError`` otherwise.
        """
        if timeout is None:
            timeout = self._remaining_timeout_fn() if self._remaining_timeout_fn else 180
        for _ in wait_until(f"log never matched {pattern!r}", timeout=timeout):
            if re.search(pattern, self.log_since(offset)):
                return self.current_log_position()

    @contextlib.contextmanager
    def log_contains(self, pattern, times=None):
        """
        Context manager that checks if the log matches pattern during the block.

        Args:
            pattern: The regex pattern to search for.
            times: If None, any number of matches is accepted.
                   If a number, exactly that many matches are required.
        """
        start_pos = self.current_log_position()
        yield
        with open(self.log) as f:
            f.seek(start_pos)
            content = f.read()
        if times is None:
            assert re.search(pattern, content), f"Pattern {pattern!r} not found in log"
        else:
            match_count = len(re.findall(pattern, content))
            assert match_count == times, (
                f"Expected {times} matches of {pattern!r}, found {match_count}"
            )

    def cleanup(self):
        """Run all registered cleanup callbacks."""
        self._cleanup_stack.close()

    def set_timeout(self, remaining_timeout_fn: Callable[[], float]) -> None:
        """
        Set the timeout function for connections.
        This is typically called by pg fixture for each test.
        """
        self._remaining_timeout_fn = remaining_timeout_fn

    def connect(self, **opts) -> PGconn:
        """
        Creates a connection to this PostgreSQL server instance.

        Args:
            **opts: Additional connection options (can override defaults)

        Returns:
            PGconn: Connected database connection

        Example:
            conn = pg.connect()
            conn = pg.connect(dbname='mydb')
        """
        if self._remaining_timeout_fn is None:
            raise RuntimeError(
                "Timeout function not set. Use set_timeout() or pg fixture."
            )

        defaults = {
            "host": self.host,
            "port": self.port,
            "dbname": "postgres",
        }
        defaults.update(opts)

        return libpq_connect(
            self.libpq_handle,
            self._cleanup_stack,
            self._remaining_timeout_fn,
            **defaults,
        )
