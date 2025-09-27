# Copyright (c) 2025, PostgreSQL Global Development Group

import datetime
import os
import pathlib
import platform
import secrets
import socket
import subprocess
import tempfile
from collections import namedtuple

import pytest

import pg
from pg.fixtures import *


@pytest.fixture(scope="session")
def cryptography():
    return pytest.importorskip("cryptography", "3.3.2")


Cert = namedtuple("Cert", "cert, certpath, key, keypath")


@pytest.fixture(scope="session")
def certs(cryptography, tmp_path_factory):
    """
    Caches commonly used certificates at the session level, and provides a way
    to create new ones.

    - certs.ca: the root CA certificate

    - certs.server: the "standard" server certficate, signed by certs.ca

    - certs.server_host: the hostname of the certs.server certificate

    - certs.new(): creates a custom certificate, signed by certs.ca
    """

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    tmpdir = tmp_path_factory.mktemp("test-certs")

    class _Certs:
        def __init__(self):
            self.ca = self.new(
                x509.Name(
                    [x509.NameAttribute(NameOID.COMMON_NAME, "PG pytest CA")],
                ),
                ca=True,
            )

            self.server_host = "example.org"
            self.server = self.new(
                x509.Name(
                    [x509.NameAttribute(NameOID.COMMON_NAME, self.server_host)],
                )
            )

        def new(self, subject: x509.Name, *, ca=False) -> Cert:
            """
            Creates and signs a new Cert with the given subject name. If ca is
            True, the certificate will be self-signed; otherwise the certificate
            is signed by self.ca.
            """
            key = rsa.generate_private_key(
                public_exponent=65537,
                key_size=2048,
            )

            builder = x509.CertificateBuilder()
            now = datetime.datetime.now(datetime.timezone.utc)

            builder = (
                builder.subject_name(subject)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now)
                .not_valid_after(now + datetime.timedelta(hours=1))
            )

            if ca:
                builder = builder.issuer_name(subject)
            else:
                builder = builder.issuer_name(self.ca.cert.subject)

            builder = builder.add_extension(
                x509.BasicConstraints(ca=ca, path_length=None),
                critical=True,
            )

            cert = builder.sign(
                private_key=key if ca else self.ca.key,
                algorithm=hashes.SHA256(),
            )

            # Dump the certificate and key to file.
            keypath = self._tofile(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ),
                suffix=".key",
            )
            certpath = self._tofile(
                cert.public_bytes(serialization.Encoding.PEM),
                suffix="-ca.crt" if ca else ".crt",
            )

            return Cert(
                cert=cert,
                certpath=certpath,
                key=key,
                keypath=keypath,
            )

        def _tofile(self, data: bytes, *, suffix) -> str:
            """
            Dumps data to a file on disk with the requested suffix and returns
            the path. The file is located somewhere in pytest's temporary
            directory root.
            """
            f = tempfile.NamedTemporaryFile(suffix=suffix, dir=tmpdir, delete=False)
            with f:
                f.write(data)

            return f.name

    return _Certs()


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
def server_instance(certs, datadir, sockdir, winpassword):
    """
    Starts a running Postgres server listening on localhost. The HBA initially
    allows only local UNIX connections from the same user.

    TODO: when installcheck is supported, this should optionally point to the
    currently running server instead.
    """

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

        subprocess.check_call(
            ["initdb", "--auth=scram-sha-256", "--pwfile", pwfile.name, datadir]
        )
        os.unlink(pwfile.name)

    else:
        # For other OSes we can just use peer auth.
        method = "peer"
        subprocess.check_call(["pg_ctl", "-D", datadir, "init"])

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

    log = os.path.join(datadir, "postgresql.log")

    with s, open(os.path.join(datadir, "postgresql.conf"), "a") as f:
        print(file=f)
        print("unix_socket_directories = '{}'".format(sockdir.as_posix()), file=f)
        print("listen_addresses = '{}'".format(",".join(addrs)), file=f)
        print("port =", port, file=f)
        print("log_connections = all", file=f)

    # Between closing of the socket, s, and server start, we're racing against
    # anything that wants to open up ephemeral ports, so try not to put any new
    # work here.

    subprocess.check_call(["pg_ctl", "-D", datadir, "-l", log, "start"])
    yield (hostaddr, port)
    subprocess.check_call(["pg_ctl", "-D", datadir, "-l", log, "stop"])
