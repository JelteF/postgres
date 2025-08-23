# Copyright (c) 2025, PostgreSQL Global Development Group

import contextlib
import os
import pathlib
import platform
import re
import shutil
import socket
import ssl
import struct
import subprocess
import tempfile
from collections import namedtuple
from typing import Dict, List, Union

import pytest

import pg

# This suite opens up local TCP ports and is hidden behind PG_TEST_EXTRA=ssl.
pytestmark = pg.require_test_extra("ssl")


#
# Test Fixtures
#


@pytest.fixture(scope="session")
def connenv(server_instance, sockdir, datadir):
    """
    Provides the values for several PG* environment variables needed for our
    utility programs to connect to the server_instance.
    """
    return {
        "PGHOST": str(sockdir),
        "PGPORT": str(server_instance[1]),
        "PGDATABASE": "postgres",
        "PGDATA": str(datadir),
    }


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
        #
        # TODO: this is less helpful if there are multiple layers, because it's
        # not clear which backup to look at. Can the backup name be printed as
        # part of the failed test output? Should we only swap on test failure?
        tmp = self._backup.parent / (self._backup.name + ".tmp")

        shutil.copyfile(self._file, tmp)
        shutil.copyfile(self._backup, self._file)
        shutil.move(tmp, self._backup)


class HBA(FileBackup):
    """
    Backs up a server's HBA configuration and provides means for temporarily
    editing it. See also pg_server, which provides an instance of this class and
    context managers for enforcing the reload/restart order of operations.
    """

    def __init__(self, datadir: pathlib.Path):
        super().__init__(datadir / "pg_hba.conf")

    def prepend(self, *lines: Union[str, List[str]]):
        """
        Temporarily prepends lines to the server's pg_hba.conf.

        As sugar for aligning HBA columns in the tests, each line can be either
        a string or a list of strings. List elements will be joined by single
        spaces before they are written to file.
        """
        with open(self._file, "r") as f:
            prior_data = f.read()

        with open(self._file, "w") as f:
            for l in lines:
                if isinstance(l, list):
                    print(*l, file=f)
                else:
                    print(l, file=f)

            f.write(prior_data)


class Config(FileBackup):
    """
    Backs up a server's postgresql.conf and provides means for temporarily
    editing it. See also pg_server, which provides an instance of this class and
    context managers for enforcing the reload/restart order of operations.
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


@pytest.fixture(scope="session")
def pg_server_session(server_instance, connenv, datadir, winpassword):
    """
    Provides common routines for configuring and connecting to the
    server_instance. For example:

        users = pg_server_session.create_users("one", "two")
        dbs = pg_server_session.create_dbs("default")

        with pg_server_session.reloading() as s:
            s.hba.prepend(["local", dbs["default"], users["two"], "peer"])

        conn = connect_somehow(**pg_server_session.conninfo)
        ...

    Attributes of note are
    - .conninfo: provides TCP connection info for the server

    This fixture unwinds its configuration changes at the end of the pytest
    session. For more granular changes, pg_server_session.subcontext() splits
    off a "nested" context to allow smaller scopes.
    """

    class _Server(contextlib.ExitStack):
        conninfo = dict(
            hostaddr=server_instance[0],
            port=server_instance[1],
        )

        # for _backup_configuration()
        _Backup = namedtuple("Backup", "conf, hba")

        def subcontext(self):
            """
            Creates a new server stack instance that can be tied to a smaller
            scope than "session".
            """
            # So far, there doesn't seem to be a need to link the two objects,
            # since HBA/Config/FileBackup operate directly on the filesystem and
            # will appear to "nest" naturally.
            return self.__class__()

        def create_users(self, *userkeys: str) -> Dict[str, str]:
            """
            Creates new users which will be dropped at the end of the server
            context.

            For each provided key, a related user name will be selected and
            stored in a map. This map is returned to let calling code look up
            the selected usernames (instead of hardcoding them and potentially
            stomping on an existing installation).
            """
            usermap = {}

            for u in userkeys:
                # TODO: use a uniquifier to support installcheck
                name = u + "user"
                usermap[u] = name

                # TODO: proper escaping
                self.psql("-c", "CREATE USER " + name)
                self.callback(self.psql, "-c", "DROP USER " + name)

            return usermap

        def create_dbs(self, *dbkeys: str) -> Dict[str, str]:
            """
            Creates new databases which will be dropped at the end of the server
            context. See create_users() for the meaning of the keys and returned
            map.
            """
            dbmap = {}

            for d in dbkeys:
                # TODO: use a uniquifier to support installcheck
                name = d + "db"
                dbmap[d] = name

                # TODO: proper escaping
                self.psql("-c", "CREATE DATABASE " + name)
                self.callback(self.psql, "-c", "DROP DATABASE " + name)

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
            try:
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
                self.callback(self.pg_ctl, "reload")
                yield self._backup_configuration()
            except:
                # We only want to reload at the end of the suite if there were
                # no errors. During exceptions, the pushed callback handles
                # things instead, so there's nothing to do here.
                raise
            else:
                # Suite completed successfully.
                self.pg_ctl("reload")

        @contextlib.contextmanager
        def restarting(self):
            """Like .reloading(), but with a full server restart."""
            try:
                self.callback(self.pg_ctl, "restart")
                yield self._backup_configuration()
            except:
                raise
            else:
                self.pg_ctl("restart")

        def psql(self, *args):
            """
            Runs psql with the given arguments. Password prompts are always
            disabled. On Windows, the admin password will be included in the
            environment.
            """
            if platform.system() == "Windows":
                pw = dict(PGPASSWORD=winpassword)
            else:
                pw = None

            self._run("psql", "-w", *args, addenv=pw)

        def pg_ctl(self, *args):
            """
            Runs pg_ctl with the given arguments. Log output will be placed in
            postgresql.log in the server's data directory.

            TODO: put the log in TESTLOGDIR
            """
            self._run("pg_ctl", "-l", str(datadir / "postgresql.log"), *args)

        def _run(self, cmd, *args, addenv: dict = None):
            # Override the existing environment with the connenv values and
            # anything the caller wanted to add. (Python 3.9 gives us the
            # less-ugly `os.environ | connenv` merge operator.)
            subenv = dict(os.environ, **connenv)
            if addenv:
                subenv.update(addenv)

            subprocess.check_call([cmd, *args], env=subenv)

        def _backup_configuration(self):
            # Wrap the existing HBA and configuration with FileBackups.
            return self._Backup(
                hba=self.enter_context(HBA(datadir)),
                conf=self.enter_context(Config(datadir)),
            )

    with _Server() as s:
        yield s


@pytest.fixture(scope="module", autouse=True)
def ssl_setup(pg_server_session, certs, datadir):
    """
    Sets up required server settings for all tests in this module. The fixture
    variable is a tuple (users, dbs) containing the user and database names that
    have been chosen for the test session.
    """
    try:
        with pg_server_session.restarting() as s:
            s.conf.set(
                ssl="on",
                ssl_ca_file=certs.ca.certpath,
                ssl_cert_file=certs.server.certpath,
                ssl_key_file=certs.server.keypath,
            )

            # Reject by default.
            s.hba.prepend("hostssl all all all reject")

    except subprocess.CalledProcessError:
        # This is a decent place to skip if the server isn't set up for SSL.
        logpath = datadir / "postgresql.log"
        unsupported = re.compile("SSL is not supported")

        with open(logpath, "r") as log:
            for line in log:
                if unsupported.search(line):
                    pytest.skip("the server does not support SSL")

        # Some other error happened.
        raise

    users = pg_server_session.create_users(
        "ssl",
    )

    dbs = pg_server_session.create_dbs(
        "ssl",
    )

    return (users, dbs)


@pytest.fixture(scope="module")
def client_cert(ssl_setup, certs):
    """
    Creates a Cert for the "ssl" user.
    """
    from cryptography import x509
    from cryptography.x509.oid import NameOID

    users, _ = ssl_setup
    user = users["ssl"]

    return certs.new(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, user)]))


@pytest.fixture
def pg_server(pg_server_session):
    """
    A per-test instance of pg_server_session. Use this fixture to make changes
    to the server which will be rolled back at the end of every test.
    """
    with pg_server_session.subcontext() as s:
        yield s


#
# Tests
#


# For use with the `creds` parameter below.
CLIENT = "client"
SERVER = "server"


@pytest.mark.parametrize(
    # fmt: off
    "auth_method,                    creds,  expected_error",
[
    # Trust allows anything.
    ("trust",                        None,   None),
    ("trust",                        CLIENT, None),
    ("trust",                        SERVER, None),

    # verify-ca allows any CA-signed certificate.
    ("trust clientcert=verify-ca",   None,   "requires a valid client certificate"),
    ("trust clientcert=verify-ca",   CLIENT, None),
    ("trust clientcert=verify-ca",   SERVER, None),

    # cert and verify-full allow only the correct certificate.
    ("trust clientcert=verify-full", None,   "requires a valid client certificate"),
    ("trust clientcert=verify-full", CLIENT, None),
    ("trust clientcert=verify-full", SERVER, "authentication failed for user"),
    ("cert",                         None,   "requires a valid client certificate"),
    ("cert",                         CLIENT, None),
    ("cert",                         SERVER, "authentication failed for user"),
],
    # fmt: on
)
def test_direct_ssl_certificate_authentication(
    pg_server,
    ssl_setup,
    certs,
    client_cert,
    remaining_timeout,
    # test parameters
    auth_method,
    creds,
    expected_error,
):
    """
    Tests direct SSL connections with various client-certificate/HBA
    combinations.
    """

    # Set up the HBA as desired by the test.
    users, dbs = ssl_setup

    user = users["ssl"]
    db = dbs["ssl"]

    with pg_server.reloading() as s:
        s.hba.prepend(
            ["hostssl", db, user, "127.0.0.1/32", auth_method],
            ["hostssl", db, user, "::1/128", auth_method],
        )

    # Configure the SSL settings for the client.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cafile=certs.ca.certpath)
    ctx.set_alpn_protocols(["postgresql"])  # for direct SSL

    # Load up a client certificate if required by the test.
    if creds == CLIENT:
        ctx.load_cert_chain(client_cert.certpath, client_cert.keypath)
    elif creds == SERVER:
        # Using a server certificate as the client credential is expected to
        # work only for clientcert=verify-ca (and `trust`, naturally).
        ctx.load_cert_chain(certs.server.certpath, certs.server.keypath)

    # Make a direct SSL connection. There's no SSLRequest in the handshake; we
    # simply wrap a TCP connection with OpenSSL.
    addr = (pg_server.conninfo["hostaddr"], pg_server.conninfo["port"])
    with socket.create_connection(addr) as s:
        s.settimeout(remaining_timeout())  # XXX this resets every operation

        with ctx.wrap_socket(s, server_hostname=certs.server_host) as conn:
            # Build and send the startup packet.
            startup_options = dict(
                user=user,
                database=db,
                application_name="pytest",
            )

            payload = b""
            for k, v in startup_options.items():
                payload += k.encode() + b"\0"
                payload += str(v).encode() + b"\0"
            payload += b"\0"  # null terminator

            pktlen = 4 + 4 + len(payload)
            conn.send(struct.pack("!IHH", pktlen, 3, 0) + payload)

            if not expected_error:
                # Expect an AuthenticationOK to come back.
                pkttype, pktlen = struct.unpack("!cI", conn.recv(5))
                assert pkttype == b"R"
                assert pktlen == 8

                authn_result = struct.unpack("!I", conn.recv(4))[0]
                assert authn_result == 0

                # Read and discard to ReadyForQuery.
                while True:
                    pkttype, pktlen = struct.unpack("!cI", conn.recv(5))
                    payload = conn.recv(pktlen - 4)

                    if pkttype == b"Z":
                        assert payload == b"I"
                        break

                # Send an empty query.
                conn.send(struct.pack("!cI", b"Q", 5) + b"\0")

                # Expect EmptyQueryResponse+ReadyForQuery.
                pkttype, pktlen = struct.unpack("!cI", conn.recv(5))
                assert pkttype == b"I"
                assert pktlen == 4

                pkttype, pktlen = struct.unpack("!cI", conn.recv(5))
                assert pkttype == b"Z"

                payload = conn.recv(pktlen - 4)
                assert payload == b"I"

            else:
                # Match the expected authentication error.
                pkttype, pktlen = struct.unpack("!cI", conn.recv(5))
                assert pkttype == b"E"

                payload = conn.recv(pktlen - 4)
                msg = None

                for component in payload.split(b"\0"):
                    if not component:
                        break  # end of message

                    key, val = component[:1], component[1:]
                    if key == b"S":
                        assert val == b"FATAL"
                    elif key == b"M":
                        msg = val.decode()

                assert re.search(expected_error, msg), "server error did not match"

            # Terminate.
            conn.send(struct.pack("!cI", b"X", 4))
