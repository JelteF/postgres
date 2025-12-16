# Copyright (c) 2025, PostgreSQL Global Development Group

import contextlib
import os
import pathlib
import platform
import shutil
import socket
import subprocess
import tempfile
from collections import namedtuple
from typing import Callable, Optional

from .util import run
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

                # TODO: proper quoting
                v = v.replace("\\", "\\\\")
                v = v.replace("'", "\\'")
                v = "'{}'".format(v)

                print(n, "=", v, file=f)


Backup = namedtuple("Backup", "conf, hba")


class PostgresServer:
    """
    Represents a running PostgreSQL server instance with management utilities.
    Provides methods for configuration, user/database creation, and server control.
    """

    def __init__(self, bindir, datadir, sockdir, winpassword, libpq_handle):
        """
        Initialize and start a PostgreSQL server instance.
        """
        self.datadir = datadir
        self.sockdir = sockdir
        self.libpq_handle = libpq_handle
        self._remaining_timeout_fn: Optional[Callable[[], float]] = None
        self._bindir = bindir
        self._winpassword = winpassword
        self._pg_ctl = os.path.join(bindir, "pg_ctl")
        self.log = os.path.join(datadir, "postgresql.log")

        initdb = os.path.join(bindir, "initdb")
        pg_ctl = self._pg_ctl

        # Lock down the HBA by default; tests can open it back up later.
        if platform.system() == "Windows":
            # On Windows, for admin connections, use SCRAM with a generated password
            # over local sockets. This requires additional work during initdb.
            method = "scram-sha-256"

            # NamedTemporaryFile doesn't work very nicely on Windows until Python
            # 3.12, which introduces NamedTemporaryFile(delete_on_close=False).
            # Until then, specify delete=False and manually unlink after use.
            with tempfile.NamedTemporaryFile("w", delete=False) as pwfile:
                pwfile.write(winpassword)

            run(initdb, "--auth=scram-sha-256", "--pwfile", pwfile.name, datadir)
            os.unlink(pwfile.name)

        else:
            # For other OSes we can just use peer auth.
            method = "peer"
            run(pg_ctl, "-D", datadir, "init")

        with open(datadir / "pg_hba.conf", "w") as f:
            print(f"# default: local {method} connections only", file=f)
            print(f"local all all {method}", file=f)

        # Figure out a port to listen on. Attempt to reserve both IPv4 and IPv6
        # addresses in one go.
        #
        # Note: socket.has_dualstack_ipv6/create_server are only in Python 3.8+.
        if hasattr(socket, "has_dualstack_ipv6") and socket.has_dualstack_ipv6():
            addr = ("::1", 0)
            s = socket.create_server(addr, family=socket.AF_INET6, dualstack_ipv6=True)

            hostaddr, port, _, _ = s.getsockname()
            addrs = [hostaddr, "127.0.0.1"]

        else:
            addr = ("127.0.0.1", 0)

            s = socket.socket()
            s.bind(addr)

            hostaddr, port = s.getsockname()
            addrs = [hostaddr]

        with s, open(os.path.join(datadir, "postgresql.conf"), "a") as f:
            print(file=f)
            print("unix_socket_directories = '{}'".format(sockdir.as_posix()), file=f)
            print("listen_addresses = '{}'".format(",".join(addrs)), file=f)
            print("port =", port, file=f)
            print("log_connections = all", file=f)
            print("datestyle = 'ISO'", file=f)
            print("timezone = 'UTC'", file=f)

        # Between closing of the socket, s, and server start, we're racing against
        # anything that wants to open up ephemeral ports, so try not to put any new
        # work here.

        run(pg_ctl, "-D", datadir, "-l", self.log, "start")

        # Read the PID file to get the postmaster PID
        with open(os.path.join(datadir, "postmaster.pid")) as f:
            pid = int(f.readline().strip())

        # Store the computed values
        self.hostaddr = hostaddr
        self.port = port
        self.pid = pid

        # ExitStack for cleanup callbacks
        self._cleanup_stack = contextlib.ExitStack()

    def psql(self, *args):
        """Run psql with the given arguments."""
        if platform.system() == "Windows":
            pw = dict(PGPASSWORD=self._winpassword)
        else:
            pw = None
        self._run(os.path.join(self._bindir, "psql"), "-w", *args, addenv=pw)

    def pg_ctl(self, *args):
        """Run pg_ctl with the given arguments."""
        self._run(self._pg_ctl, "-l", self.log, *args)

    def _run(self, cmd, *args, addenv: Optional[dict] = None):
        """Run a command with PG* environment variables set."""
        subenv = dict(os.environ)
        subenv.update(
            {
                "PGHOST": str(self.sockdir),
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

    def stop(self):
        """
        Stop the PostgreSQL server instance.

        Ignores failures if the server is already stopped.
        """
        try:
            run(self._pg_ctl, "-D", self.datadir, "-l", self.log, "stop")
        except subprocess.CalledProcessError:
            # Server may have already been stopped
            pass

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

        This is a convenience method that automatically fills in the host, port,
        and dbname (defaulting to 'postgres') for connecting to this server.

        Args:
            stack: ExitStack for managing connection cleanup (uses internal stack if not provided)
            remaining_timeout_fn: Function that returns remaining timeout (uses stored timeout if not provided)
            **opts: Additional connection options (can override defaults)

        Returns:
            PGconn: Connected database connection

        Example:
            conn = pg.connect()
            conn = pg.connect(dbname='mydb')
        """
        # Set default connection options for this server
        defaults = {
            "host": str(self.sockdir),
            "port": self.port,
            "dbname": "postgres",
        }

        # On Windows, include the password for SCRAM authentication
        if platform.system() == "Windows" and self._winpassword:
            defaults["password"] = self._winpassword

        # Merge with user-provided options (user options take precedence)
        defaults.update(opts)

        if self._remaining_timeout_fn is None:
            raise RuntimeError(
                "Timeout function not set. Use set_timeout() or pg fixture."
            )

        return libpq_connect(
            self.libpq_handle,
            self._cleanup_stack,
            self._remaining_timeout_fn,
            **defaults,
        )
