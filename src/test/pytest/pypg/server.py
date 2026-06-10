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
import threading
from collections import namedtuple
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Optional

from .util import capture, run, wait_until
from libpq import PGconn, connect as libpq_connect


def _copy_command(archive_dir, dest):
    """Build an archive_command/restore_command that copies a WAL segment.

    With ``dest=False`` the segment (``%p``) is copied into ``archive_dir``
    (archive_command); with ``dest=True`` it is copied out of ``archive_dir``
    into ``%p`` (restore_command).

    Mirrors Perl's enable_archiving()/enable_restoring(): on Windows use cmd's
    ``copy`` with backslash paths (postgres hands ``%p``/``%f`` to the command
    as backslash paths, which a Unix ``cp`` would mangle), doubling the
    backslashes so the path survives archive_command processing.
    """
    if platform.system() == "Windows":
        path = str(archive_dir).replace("\\", "\\\\")
        archived = f'{path}\\\\%f'
        return f'copy "{archived}" "%p"' if dest else f'copy "%p" "{archived}"'
    archived = f"{archive_dir}/%f"
    return f'cp "{archived}" "%p"' if dest else f'cp "%p" "{archived}"'


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


class BackgroundConnection:
    """A persistent libpq session that can run queries in the background.

    This is the pytest replacement for Perl's ``background_psql``. Like a
    background psql process it keeps a single connection open, so session state
    (open transactions, held locks, session-local settings) persists across
    calls. Obtain one from ``PostgresServer.background()``.

    ``sql()`` runs a query to completion, just like ``PostgresServer.sql()``
    but on the held connection (Perl's ``query_safe``). ``asql()`` dispatches a
    query that is expected to *block* — on a lock or an injection point — and
    returns a :class:`concurrent.futures.Future` that is already running, so
    the test can carry on (e.g. observe the wait with ``wait_for_event()``,
    then release it) and call ``.result()`` on the future to collect the
    outcome once it unblocks.

    A future is returned rather than a coroutine on purpose: the query starts
    immediately and runs concurrently, instead of only running once awaited.

    All queries run on a single worker thread, so they serialize on the one
    connection: calling ``sql()``/``asql()`` while a previous ``asql()`` is
    still blocked will queue behind it, exactly as a real session would.
    """

    def __init__(self, conn):
        self._conn = conn
        self._executor = ThreadPoolExecutor(max_workers=1)

    def asql(self, query) -> Future:
        """Dispatch ``query`` on the session and return a running Future.

        Resolve it with ``.result()`` (which re-raises any ``LibpqError``).
        """
        return self._executor.submit(self._conn.sql, query)

    def sql(self, query):
        """Run ``query`` on the session and return its result, like
        ``PostgresServer.sql()`` but on the persistent connection."""
        return self.asql(query).result()

    def notifies(self):
        """Return and consume pending LISTEN/NOTIFY notifications on this
        session (see ``PGconn.notifies``). Runs on the session's worker thread
        so it serializes with its queries."""
        return self._executor.submit(self._conn.notifies).result()

    def quit(self):
        """End the session cleanly, like Perl's ``$session->quit``: wait for
        any in-flight query, then disconnect. Disconnecting runs the backend's
        session-exit cleanup, so this is also how a test deliberately triggers
        that cleanup (e.g. to drop the session's temp objects)."""
        self._executor.shutdown(wait=True)
        self._conn.close()

    def close(self):
        """Shut the session down without waiting (teardown path): drop the
        worker thread and let the connection close with its owning server's
        cleanup."""
        self._executor.shutdown(wait=False, cancel_futures=True)


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
        initdb_opts: Optional[list] = None,
        from_backup: Optional[pathlib.Path] = None,
        streaming_primary: Optional["PostgresServer"] = None,
        allows_streaming: bool = False,
        archiving: bool = False,
        restoring: Optional["PostgresServer"] = None,
        restoring_standby: bool = True,
        conf: Optional[list] = None,
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
            initdb_opts: Extra arguments to pass to initdb (e.g.
                ["--locale=C", "--encoding=LATIN1"]). When provided the fast
                INITDB_TEMPLATE copy is bypassed and a real initdb is run, since
                the template was created with the default locale/encoding.
            from_backup: Path to a base backup (as produced by
                ``PostgresServer.backup()``) to copy into the data directory
                instead of running initdb. Use this to build a standby or a
                point-in-time-recovery node.
            streaming_primary: When building from a backup, the upstream server
                to stream WAL from. Sets ``primary_conninfo`` and creates a
                ``standby.signal`` file so the node starts as a streaming
                standby. Its ``application_name`` is this node's name, so the
                primary can ``wait_for_catchup()`` on it by name.
            allows_streaming: Configure this server to act as a replication
                primary (``wal_log_hints`` plus generous ``max_wal_senders`` /
                ``max_replication_slots``). The defaults already permit basic
                streaming; this mirrors Perl's ``init(allows_streaming => 1)``.
            archiving: Enable WAL archiving (``archive_mode = on`` plus an
                ``archive_command`` that copies segments into this server's
                ``archive_dir``). Mirrors Perl's ``enable_archiving``.
            restoring: When building from a backup, the upstream server whose
                ``archive_dir`` to restore WAL from (sets ``restore_command``).
                Mirrors Perl's ``init_from_backup(..., has_restoring => 1)``,
                which defaults ``standby => 1``, so by default a
                ``standby.signal`` is dropped (the node keeps replaying archived
                WAL as a standby). Pass ``restoring_standby=False`` for a
                ``recovery.signal`` instead (archive recovery that promotes when
                WAL runs out). Either way, setting a recovery target via ``conf``
                with ``recovery_target_action = promote`` performs PITR.
            restoring_standby: See ``restoring``.
            conf: Extra postgresql.conf lines to append before the first start.
                Use for settings that must be present at startup, such as a
                point-in-time recovery target (``recovery_target_lsn`` etc.),
                since ``create_pg`` starts the server immediately.
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
        # Where base backups taken from this server are written.
        self._backup_root = pathlib.Path(datadir).parent / f"{name}_backups"
        # Where archived WAL segments are written (when archiving is enabled),
        # and the source a restoring node reads from.
        self.archive_dir = pathlib.Path(datadir).parent / f"{name}_archive"

        # ExitStack for cleanup callbacks
        self._cleanup_stack = contextlib.ExitStack()

        # Determine whether to use Unix sockets
        use_unix_sockets = platform.system() != "Windows" and hostaddr is None

        # A backup-based node copies the backup into place rather than running
        # initdb. The backup carries the primary's config; the conf appended
        # below (port, sockets, ...) overrides it since later entries win.
        if from_backup is not None:
            shutil.copytree(from_backup, datadir)
            os.chmod(datadir, 0o700)
        # Use INITDB_TEMPLATE if available (much faster than running initdb),
        # unless caller-supplied initdb options require a real initdb.
        elif (initdb_template := os.environ.get("INITDB_TEMPLATE")) and (
            not initdb_opts and os.path.isdir(initdb_template)
        ):
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
                *(initdb_opts or []),
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

        # Replication-primary settings, mirroring init(allows_streaming => 1).
        # wal_level/max_wal_senders/hot_standby already default to streaming-
        # capable values; wal_log_hints (off by default) is the one that
        # matters for pg_rewind-style tests.
        if allows_streaming:
            self.append_conf(
                "wal_level = replica",
                "max_wal_senders = 10",
                "max_replication_slots = 10",
                "wal_log_hints = on",
                "hot_standby = on",
                "max_wal_size = 128MB",
            )

        # Configure streaming replication from the primary, mirroring Perl's
        # enable_streaming(): set primary_conninfo and drop a standby.signal so
        # the node comes up as a streaming standby.
        if streaming_primary is not None:
            conninfo = streaming_primary.connstr(application_name=self.name)
            self.append_conf(f"primary_conninfo = '{conninfo}'")
            self.append_conf(filename="standby.signal")

        # Enable WAL archiving, mirroring Perl's enable_archiving(). archive_mode
        # is a postmaster GUC, so this must be configured before the first start.
        if archiving:
            os.makedirs(self.archive_dir, exist_ok=True)
            self.append_conf(
                "archive_mode = on",
                f"archive_command = '{_copy_command(self.archive_dir, dest=False)}'",
                "wal_level = replica",
            )

        # Restore WAL from an upstream server's archive, mirroring Perl's
        # enable_restoring(). A standby.signal keeps the node replaying the
        # archive as a standby (the init_from_backup default); a recovery.signal
        # makes it perform archive recovery and promote when WAL runs out.
        if restoring is not None:
            self.append_conf(
                f"restore_command = '{_copy_command(restoring.archive_dir, dest=True)}'"
            )
            signal = "standby.signal" if restoring_standby else "recovery.signal"
            self.append_conf(filename=signal)

        # Caller-supplied startup config (e.g. a recovery target).
        if conf:
            self.append_conf(*conf)

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

    def promote(self):
        """Promote a standby/recovery node to a primary, waiting for the
        promotion to finish (pg_ctl promote -w). Mirrors Perl's ``$node->promote``.
        """
        self.pg_ctl("promote", "-w")

    def enable_streaming(self, primary: "PostgresServer"):
        """Reconfigure this (stopped) node to stream from ``primary`` as a
        standby: set ``primary_conninfo`` and drop a ``standby.signal``. Mirrors
        Perl's ``$node->enable_streaming``. Use it to re-attach a former primary
        as a standby of a newly-promoted node (a role swap); call ``start()``
        afterwards. The standby's ``application_name`` is this node's name so the
        new primary can ``wait_for_catchup()`` on it by name.
        """
        conninfo = primary.connstr(application_name=self.name)
        self.append_conf(f"primary_conninfo = '{conninfo}'")
        self.append_conf(filename="standby.signal")

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

    def sql(self, query, dbname="postgres"):
        """Execute a SQL query via libpq. Returns simplified results."""
        with self.connect(dbname=dbname) as conn:
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

    def adjust_conf(self, setting, value=None, filename="postgresql.conf"):
        """Set ``setting`` to ``value`` in a config file, replacing any existing
        (or commented-out) lines for it; if ``value`` is None, remove them.

        Unlike append_conf this leaves a single clean line for the setting
        rather than relying on later-line-wins. Mirrors Perl's
        ``$node->adjust_conf``. Like append_conf it does not reload the server.
        """
        path = self.datadir / filename
        pat = re.compile(rf"^\s*#?\s*{re.escape(setting)}\s*=")
        lines = [ln for ln in path.read_text().splitlines() if not pat.match(ln)]
        if value is not None:
            lines.append(f"{setting} = {value}")
        path.write_text("\n".join(lines) + "\n")

    def reset_hba(self, database, role, method):
        """Replace pg_hba.conf with a single local rule and reload the server.

        Mirrors the ``reset_pg_hba`` helper duplicated across the authentication
        TAP tests. The rule is written across a continuation line, which also
        exercises pg_hba.conf continuation-line parsing just like the Perl
        original did.
        """
        hba = self.datadir / "pg_hba.conf"
        hba.write_text(f"local {database} {role}\\\n {method}\n")
        self.pg_ctl("reload")

    def reset_ident(self, map_name, system_user, pg_user):
        """Replace pg_ident.conf with a single user-name-map entry and reload.

        Mirrors the ``reset_pg_ident`` helper in the peer authentication TAP
        test.
        """
        ident = self.datadir / "pg_ident.conf"
        ident.write_text(f"{map_name} {system_user} {pg_user}\n")
        self.pg_ctl("reload")

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

    def connection_env(self):
        """Return the PG* environment variables that point a client program at
        this server.

        Use this to run an installed client program (createdb, vacuumdb, ...)
        against this server while capturing its output, e.g. via
        ``pg_bin.run(name, ..., server=pg)``.
        """
        return {
            "PGHOST": str(self.host),
            "PGPORT": str(self.port),
            "PGDATABASE": "postgres",
            "PGDATA": str(self.datadir),
        }

    def connstr(self, dbname="postgres", **opts):
        """Return a libpq connection string pointing at this server.

        Extra keyword options (e.g. ``application_name``) are appended. Used
        for ``primary_conninfo`` on standbys and by replication clients.
        """
        parts = [f"host={self.host}", f"port={self.port}", f"dbname={dbname}"]
        parts += [f"{k}={v}" for k, v in opts.items()]
        return " ".join(parts)

    def backup(self, backup_name="my_backup", backup_options=None):
        """Take a base backup of this (running) server with pg_basebackup.

        The backup is written under a per-server backups directory and the path
        is returned, suitable for passing as ``from_backup`` when creating a
        standby. Mirrors Perl's ``$node->backup()``.
        """
        backup_path = self._backup_root / backup_name
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        run(
            self._bindir / "pg_basebackup",
            "--no-sync",
            "--pgdata", backup_path,
            "--host", self.host,
            "--port", str(self.port),
            "--checkpoint", "fast",
            *(backup_options or []),
        )
        return backup_path

    def pg_recvlogical_upto(self, slot_name, endpos, *, dbname="postgres",
                            timeout=None, options=None):
        """Stream a logical slot's changes up to ``endpos`` with pg_recvlogical.

        Runs ``pg_recvlogical --start`` (which confirms the changes it reads,
        advancing the slot) and returns its stdout as text with the trailing
        newline stripped. ``options`` is a dict of plugin output options, each
        passed as ``--option name=value``. Mirrors Perl's
        ``$node->pg_recvlogical_upto``.
        """
        args = [
            self._bindir / "pg_recvlogical",
            "--slot", slot_name,
            "--dbname", self.connstr(dbname),
            "--endpos", endpos,
            "--file", "-",
            "--no-loop", "--start",
        ]
        for k, v in (options or {}).items():
            args.append("--option")
            args.append(f"{k}={v}")
        return capture(*args, timeout=timeout)

    def advance_wal(self, num):
        """Advance WAL by ``num`` segments.

        Emits an empty logical message and forces a segment switch ``num``
        times. ``pg_switch_wal()`` flushes WAL, so ``pg_logical_emit_message()``
        is safe in non-transactional mode. Mirrors Perl's ``$node->advance_wal``.
        """
        with self.connect() as conn:
            for _ in range(num):
                conn.sql("SELECT pg_logical_emit_message(false, '', 'foo')")
                conn.sql("SELECT pg_switch_wal()")

    def _get_insert_lsn(self):
        """Return the current insert LSN of this server, in bytes."""
        return int(self.sql("SELECT pg_current_wal_insert_lsn() - '0/0'"))

    def emit_wal(self, size):
        """Emit a transactional logical message of ``size`` bytes and return the
        resulting end LSN, in bytes. Mirrors Perl's ``$node->emit_wal``."""
        return int(
            self.sql(f"SELECT pg_logical_emit_message(true, '', repeat('a', {size})) - '0/0'")
        )

    def write_wal(self, tli, lsn, segment_size, data: bytes):
        """Write ``data`` (bytes) into the WAL segment file at byte ``lsn`` on
        timeline ``tli``, returning the segment path. Used to corrupt WAL on a
        stopped server. Mirrors Perl's ``$node->write_wal``."""
        segment = lsn // segment_size
        offset = lsn % segment_size
        path = pathlib.Path(self.datadir) / "pg_wal" / f"{tli:08X}{0:08X}{segment:08X}"
        with open(path, "r+b") as f:
            f.seek(offset)
            f.write(data)
        return path

    def advance_wal_out_of_record_splitting_zone(self, wal_block_size):
        """Advance WAL to a safe distance from the end of a page (enough to fit
        a couple of small records), returning the end LSN in bytes. Mirrors
        Perl's ``$node->advance_wal_out_of_record_splitting_zone``."""
        page_threshold = wal_block_size // 4
        end_lsn = self._get_insert_lsn()
        page_offset = end_lsn % wal_block_size
        while page_offset >= wal_block_size - page_threshold:
            self.emit_wal(page_threshold)
            end_lsn = self._get_insert_lsn()
            page_offset = end_lsn % wal_block_size
        return end_lsn

    def advance_wal_to_record_splitting_zone(self, wal_block_size):
        """Advance WAL so close to the end of a page that an XLogRecordHeader
        would not fit on it, returning the end LSN in bytes. Mirrors Perl's
        ``$node->advance_wal_to_record_splitting_zone``."""
        record_header_size = 24
        end_lsn = self._get_insert_lsn()
        page_offset = end_lsn % wal_block_size

        # Get fairly close to the end of a page in big steps.
        while page_offset <= wal_block_size - 512:
            self.emit_wal(wal_block_size - page_offset - 256)
            end_lsn = self._get_insert_lsn()
            page_offset = end_lsn % wal_block_size

        # Calibrate the message size so we can get closer 8 bytes at a time.
        message_size = wal_block_size - 80
        while page_offset <= wal_block_size - record_header_size:
            self.emit_wal(message_size)
            end_lsn = self._get_insert_lsn()
            old_offset = page_offset
            page_offset = end_lsn % wal_block_size
            # Adjust the message size until it causes 8-byte changes in offset,
            # enough to be able to split a record header.
            delta = page_offset - old_offset
            if delta > 8:
                message_size -= 8
            elif delta <= 0:
                message_size += 8
        return end_lsn

    def backup_fs_cold(self, backup_name="cold_backup"):
        """Take a filesystem-level cold backup of this (stopped) server.

        Copies the whole data directory, including WAL, into a per-server
        backups directory and returns the path, suitable for ``from_backup``.
        The server must be stopped, as no attempt is made to handle concurrent
        writes; a node restored from such a backup enters crash recovery before
        switching to archive recovery. Mirrors Perl's ``$node->backup_fs_cold``.
        """
        backup_path = self._backup_root / backup_name
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            self.datadir,
            backup_path,
            ignore=shutil.ignore_patterns(
                "postmaster.pid", "postmaster.opts", "postgresql.log"
            ),
        )
        return backup_path

    def lsn(self, mode="write"):
        """Return a current WAL LSN of this server as a string.

        ``mode`` selects the function: ``insert``/``flush``/``write`` on a
        primary, ``receive``/``replay`` on a standby. Mirrors ``$node->lsn()``.
        """
        funcs = {
            "insert": "pg_current_wal_insert_lsn()",
            "flush": "pg_current_wal_flush_lsn()",
            "write": "pg_current_wal_lsn()",
            "receive": "pg_last_wal_receive_lsn()",
            "replay": "pg_last_wal_replay_lsn()",
        }
        return self.sql(f"SELECT {funcs[mode]}")

    def wait_for_catchup(self, standby_name, mode="replay", target_lsn=None):
        """Wait until a streaming standby has caught up to ``target_lsn``.

        Polls pg_stat_replication on this (upstream) server until the standby's
        ``<mode>_lsn`` has reached ``target_lsn`` (the upstream's current write
        LSN by default) while in the ``streaming`` state. ``standby_name`` is
        matched against ``application_name`` (or the default ``walreceiver``).
        Mirrors Perl's ``$node->wait_for_catchup()``.
        """
        if isinstance(standby_name, PostgresServer):
            standby_name = standby_name.name
        if target_lsn is None:
            # On a standby (e.g. a standby acting as a publisher) the write LSN
            # isn't available; use the replay LSN, like Perl's wait_for_catchup.
            if self.sql("SELECT pg_is_in_recovery()"):
                target_lsn = self.lsn("replay")
            else:
                target_lsn = self.lsn("write")
        query = (
            f"SELECT '{target_lsn}' <= {mode}_lsn AND state = 'streaming' "
            "FROM pg_catalog.pg_stat_replication "
            f"WHERE application_name IN ('{standby_name}', 'walreceiver')"
        )
        self.poll_query_until(query, True)

    def wait_for_slot_catchup(self, slot_name, mode="restart", target_lsn=None):
        """Wait until a replication slot's ``<mode>_lsn`` reaches ``target_lsn``.

        Polls pg_replication_slots on this server. ``mode`` is ``restart`` or
        ``confirmed_flush``. Mirrors Perl's ``$node->wait_for_slot_catchup()``.
        """
        assert target_lsn is not None, "target lsn must be specified"
        assert mode in ("restart", "confirmed_flush")
        self.poll_query_until(
            f"SELECT '{target_lsn}' <= {mode}_lsn "
            f"FROM pg_catalog.pg_replication_slots WHERE slot_name = '{slot_name}'"
        )

    def wait_for_subscription_sync(self, publisher=None, subname=None, dbname="postgres"):
        """Wait for a subscription's initial table sync to finish, then for the
        subscriber to catch up to the publisher.

        Called on the subscriber: polls pg_subscription_rel until every table is
        synced (``r``/``s``). If ``publisher`` and ``subname`` are given, also
        waits for the publisher's walsender (named after the subscription) to
        catch up. Mirrors Perl's ``$node->wait_for_subscription_sync()``.
        """
        self.poll_query_until(
            "SELECT count(1) = 0 FROM pg_subscription_rel "
            "WHERE srsubstate NOT IN ('r', 's')",
            dbname=dbname,
        )
        if publisher is not None and subname is not None:
            publisher.wait_for_catchup(subname)

    @contextlib.contextmanager
    def repeat_query(self, query, interval=0.1, dbname="postgres"):
        """Context manager that runs ``query`` repeatedly in the background on
        its own connection until the block exits, like psql's ``\\watch``.

        Used to keep generating activity (e.g. transactions producing running
        xact records) while the test does other work. Errors from a connection
        torn down by a deliberate stop/restart are swallowed.
        """
        stop = threading.Event()
        conn = self.connect(dbname=dbname)

        def loop():
            while not stop.is_set():
                try:
                    conn.sql(query)
                except Exception:
                    return
                stop.wait(interval)

        worker = threading.Thread(target=loop, daemon=True)
        worker.start()
        try:
            yield
        finally:
            stop.set()
            worker.join(timeout=10)
            try:
                conn.close()
            except Exception:
                pass

    def log_standby_snapshot(self, standby, slot_name):
        """Emit the ``xl_running_xacts`` record a standby's logical slot
        creation is waiting for.

        Called on the primary: waits until the standby slot's ``restart_lsn`` is
        determined, then runs ``pg_log_standby_snapshot()``. Mirrors Perl's
        ``$primary->log_standby_snapshot()``.
        """
        standby.poll_query_until(
            "SELECT restart_lsn IS NOT NULL FROM pg_catalog.pg_replication_slots "
            f"WHERE slot_name = '{slot_name}'"
        )
        self.sql("SELECT pg_log_standby_snapshot()")

    def create_logical_slot_on_standby(self, primary, slot_name, dbname="postgres"):
        """Create a logical replication slot on this standby.

        Logical slot creation on a standby blocks until an ``xl_running_xacts``
        record arrives, so it is driven from a background ``pg_recvlogical
        --create-slot`` while the primary is asked to log a standby snapshot.
        Mirrors Perl's ``$standby->create_logical_slot_on_standby()``.
        """
        recv = subprocess.Popen(
            [
                str(self._bindir / "pg_recvlogical"),
                "--dbname", self.connstr(dbname),
                "--plugin", "test_decoding",
                "--slot", slot_name,
                "--create-slot",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Arrange for the xl_running_xacts record pg_recvlogical waits for.
        primary.log_standby_snapshot(self, slot_name)
        recv.wait()
        assert (
            self.sql(
                "SELECT slot_type FROM pg_catalog.pg_replication_slots "
                f"WHERE slot_name = '{slot_name}'"
            )
            == "logical"
        ), f"{slot_name} on standby created"

    def background(self, dbname="postgres", **opts) -> BackgroundConnection:
        """Open a persistent background session against this server.

        Returns a :class:`BackgroundConnection` (the replacement for Perl's
        ``background_psql``) that holds its connection open until the server is
        cleaned up, so it can keep a transaction or lock alive across steps and
        dispatch blocking queries with ``asql()``.
        """
        conn = self.connect(dbname=dbname, **opts)
        bg = BackgroundConnection(conn)
        self._cleanup_stack.callback(bg.close)
        return bg

    def wait_for_event(self, backend_type, wait_event):
        """Wait until some backend is parked on a given wait event.

        Polls pg_stat_activity until a backend of ``backend_type`` reports
        ``wait_event``. Use it after dispatching a blocking query with
        ``BackgroundConnection.asql()`` to confirm it has reached the expected
        wait point. Mirrors Perl's ``wait_for_event()``.
        """
        self.poll_query_until(
            "SELECT count(*) > 0 FROM pg_stat_activity "
            f"WHERE backend_type = '{backend_type}' AND wait_event = '{wait_event}'",
            True,
        )

    def wait_for_injection_point(self, name):
        """Wait until some backend is parked at the named injection point.

        Polls pg_stat_activity for a backend whose wait event is the injection
        point (``wait_event_type = 'InjectionPoint'``). Use after dispatching a
        query with ``BackgroundConnection.asql()`` that is expected to block on
        a point attached in ``'wait'`` mode. Mirrors Perl's
        ``wait_for_injection_point()``. Unlike ``wait_for_event()`` it does not
        constrain the backend type, so it also catches background workers
        (autovacuum, checkpointer, ...) parked at the point.
        """
        self.poll_query_until(
            "SELECT count(*) > 0 FROM pg_stat_activity "
            f"WHERE wait_event_type = 'InjectionPoint' AND wait_event = '{name}'",
            True,
        )

    def _run(self, cmd, *args, addenv: Optional[dict] = None):
        """Run a command with PG* environment variables set."""
        subenv = dict(os.environ)
        subenv.update(self.connection_env())
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
