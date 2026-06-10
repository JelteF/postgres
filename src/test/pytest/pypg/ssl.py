# Copyright (c) 2025, PostgreSQL Global Development Group

"""SSL test-server configuration helper.

This is the pytest replacement for the Perl ``SSL::Server`` /
``SSL::Backend::OpenSSL`` modules used by ``src/test/ssl``. It configures an
existing (TCP) test cluster for the SSL regression tests: installs the
committed test certificates/keys into the data directory, creates the standard
set of users and databases, writes the ``hostssl`` ``pg_hba.conf`` rules and
the ident maps, and switches the active server certificate.

The certificate sources live in ``src/test/ssl/ssl`` (committed test data),
located relative to this module. Client private keys are copied into a
per-test temporary directory with the restrictive permissions libpq requires;
``sslkey()`` returns the path to use in a connection string.

Only the OpenSSL backend is supported, matching the Perl suite.
"""

import glob
import os
import pathlib
import shutil

# src/test/pytest/pypg/ssl.py -> parents[2] == src/test
_SSL_SRC = pathlib.Path(__file__).resolve().parents[2] / "ssl" / "ssl"

# Client private keys that need a permissions-corrected copy outside the source
# tree (the source files may be world-readable in a checkout).
_CLIENT_KEYS = (
    "client.key",
    "client-revoked.key",
    "client-der.key",
    "client-encrypted-pem.key",
    "client-encrypted-der.key",
    "client-dn.key",
    "client_ext.key",
    "client-long.key",
    "client-revoked-utf8.key",
)

_DATABASES = ("trustdb", "certdb", "certdb_dn", "certdb_dn_re", "certdb_cn", "verifydb")


class SSLServer:
    """Configures a test cluster for SSL, mirroring Perl's ``SSL::Server``."""

    def __init__(self, keydir):
        self.ssldir = _SSL_SRC
        self._keydir = pathlib.Path(keydir)
        self._keys = {}

    def cert(self, name):
        """Absolute path to a file in the certificate source directory, for use
        as ``sslcert``/``sslrootcert``/``sslcrl`` in a connection string."""
        return str(self.ssldir / name)

    def sslkey(self, name):
        """Absolute path to the permissions-corrected copy of a client key."""
        return self._keys[name]

    def _install_certs(self, pgdata):
        """Install server certs/keys, CA certs and CRLs into the data directory,
        and stage client keys with correct permissions (Perl's OpenSSL init)."""
        pgdata = pathlib.Path(pgdata)

        def copy_into(pattern, dest):
            for src in glob.glob(str(self.ssldir / pattern)):
                shutil.copy(src, dest / os.path.basename(src))

        copy_into("server-*.crt", pgdata)
        copy_into("server-*.key", pgdata)
        for key in glob.glob(str(pgdata / "server-*.key")):
            os.chmod(key, 0o600)
        for name in (
            "root+client_ca.crt",
            "root+server_ca.crt",
            "root_ca.crt",
            "root+client.crl",
        ):
            shutil.copy(self.ssldir / name, pgdata / name)

        crldir = pgdata / "root+client-crldir"
        crldir.mkdir()
        copy_into("root+client-crldir/*", crldir)

        # Client private keys must not be world-readable; copy them into a
        # temporary directory and fix permissions there.
        self._keydir.mkdir(parents=True, exist_ok=True)
        for keyfile in _CLIENT_KEYS:
            dest = self._keydir / keyfile
            shutil.copy(self.ssldir / keyfile, dest)
            os.chmod(dest, 0o600)
            self._keys[keyfile] = str(dest)

        # An explicitly world-readable copy of client.key, to test that libpq
        # rejects insecure key permissions.
        wrongperms = self._keydir / "client_wrongperms.key"
        shutil.copy(self.ssldir / "client.key", wrongperms)
        os.chmod(wrongperms, 0o644)
        self._keys["client_wrongperms.key"] = str(wrongperms)

    def configure_test_server_for_ssl(
        self,
        node,
        serverhost="127.0.0.1",
        servercidr="127.0.0.1/32",
        authmethod="trust",
        *,
        password=None,
        password_enc=None,
        extensions=None,
    ):
        """Configure ``node`` to accept SSL connections.

        Creates the trustdb/certdb/... databases and the ssltestuser/...
        users (optionally with a password), installs the test certificates,
        enables SSL via an included ``sslconfig.conf``, restarts, and writes the
        ``hostssl`` HBA rules plus ident maps. The HBA rules are not reloaded
        here (``hostssl`` requires ``ssl=on``); the following
        ``switch_server_cert()`` turns SSL on and restarts.
        """
        pgdata = pathlib.Path(node.datadir)

        # Create test users and databases.
        for user in ("ssltestuser", "md5testuser", "anotheruser", "yetanotheruser"):
            node.sql(f"CREATE USER {user}")
        for db in _DATABASES:
            node.sql(f"CREATE DATABASE {db}")

        # Update passwords as needed.
        if password is not None:
            assert password_enc is not None, (
                "Password encryption must be specified when password is set"
            )
            node.sql(
                f"SET password_encryption='{password_enc}'; "
                f"ALTER USER ssltestuser PASSWORD '{password}';"
            )
            # A special user that always has an md5-encrypted password.
            node.sql(
                f"SET password_encryption='md5'; "
                f"ALTER USER md5testuser PASSWORD '{password}';"
            )
            node.sql(
                f"SET password_encryption='{password_enc}'; "
                f"ALTER USER anotheruser PASSWORD '{password}';"
            )

        # Create any requested extensions in every database.
        for extension in extensions or []:
            for db in _DATABASES:
                node.sql(f"CREATE EXTENSION {extension} CASCADE;", dbname=db)

        # Enable logging etc.
        node.append_conf(
            "fsync=off",
            "log_connections=all",
            "log_hostname=on",
            f"listen_addresses='{serverhost}'",
            "log_statement=all",
            # SSL configuration is placed in this file by switch_server_cert.
            "include 'sslconfig.conf'",
        )
        (pgdata / "sslconfig.conf").write_text("")

        # Install certificates and keys.
        self._install_certs(pgdata)

        # Restart to load new listen_addresses.
        node.pg_ctl("restart")

        # Change pg_hba after restart because hostssl requires ssl=on (turned on
        # by the subsequent switch_server_cert).
        self._configure_hba_for_ssl(node, servercidr, authmethod)

    def switch_server_cert(
        self,
        node,
        *,
        certfile,
        cafile="root+client_ca",
        keyfile=None,
        crlfile="root+client.crl",
        crldir=None,
        passphrase_cmd=None,
        passphrase_cmd_reload=None,
        restart=True,
    ):
        """Point the server at a different certificate/key/CRL set and restart.

        Rewrites ``sslconfig.conf`` with ``ssl=on`` and the relevant GUCs (cert,
        key, CRL, CA, plus ECDH-curve and TLS1.3-cipher lists for syntax
        coverage). Pass ``restart=False`` to leave the server running.
        """
        if keyfile is None:
            keyfile = certfile
        pgdata = pathlib.Path(node.datadir)

        lines = [
            "ssl=on",
            f"ssl_cert_file='{certfile}.crt'",
            f"ssl_key_file='{keyfile}.key'",
            f"ssl_crl_file='{crlfile}'",
        ]
        if cafile != "":
            lines.append(f"ssl_ca_file='{cafile}.crt'")
        else:
            lines.append("ssl_ca_file=''")
        if crldir is not None:
            lines.append(f"ssl_crl_dir='{crldir}'")
        # Use lists of ECDH curves and cipher suites for syntax testing.
        lines.append("ssl_groups=prime256v1:secp521r1")
        lines.append(
            "ssl_tls13_ciphers=TLS_AES_256_GCM_SHA384:TLS_AES_128_GCM_SHA256"
        )
        if passphrase_cmd is not None:
            lines.append(f"ssl_passphrase_command='{passphrase_cmd}'")
        if passphrase_cmd_reload is not None:
            lines.append(
                f"ssl_passphrase_command_supports_reload='{passphrase_cmd_reload}'"
            )

        (pgdata / "sslconfig.conf").write_text("\n".join(lines) + "\n")

        if restart:
            node.pg_ctl("restart")

    def _configure_hba_for_ssl(self, node, servercidr, authmethod):
        """Rewrite pg_hba.conf with hostssl rules and pg_ident.conf with the
        cert-name maps. Not reloaded here; see configure_test_server_for_ssl."""
        pgdata = pathlib.Path(node.datadir)
        (pgdata / "pg_hba.conf").write_text(
            "# TYPE  DATABASE      USER            ADDRESS       METHOD         OPTIONS\n"
            f"hostssl trustdb       md5testuser     {servercidr}   md5\n"
            f"hostssl trustdb       all             {servercidr}   {authmethod}\n"
            f"hostssl verifydb      ssltestuser     {servercidr}   {authmethod}    clientcert=verify-full\n"
            f"hostssl verifydb      anotheruser     {servercidr}   {authmethod}    clientcert=verify-full\n"
            f"hostssl verifydb      yetanotheruser  {servercidr}   {authmethod}    clientcert=verify-ca\n"
            f"hostssl certdb        all             {servercidr}   cert\n"
            f"hostssl certdb_dn     all             {servercidr}   cert clientname=DN map=dn\n"
            f"hostssl certdb_dn_re  all             {servercidr}   cert clientname=DN map=dnre\n"
            f"hostssl certdb_cn     all             {servercidr}   cert clientname=CN map=cn\n"
        )
        # Note: fields with commas must be quoted.
        (pgdata / "pg_ident.conf").write_text(
            "# MAPNAME SYSTEM-USERNAME                                         PG-USERNAME\n"
            'dn        "CN=ssltestuser-dn,OU=Testing,OU=Engineering,O=PGDG"    ssltestuser\n'
            'dnre      "/^.*OU=Testing,.*$"                                   ssltestuser\n'
            "cn        ssltestuser-dn                                          ssltestuser\n"
        )
