# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Port of src/test/ssl/t/004_sni.pl.

Tests Server Name Indication (SNI) support via pg_hosts.conf: serving different
certificates/CAs per requested host name, the precedence between
postgresql.conf and pg_hosts.conf, default vs. host-specific vs. no-SNI
entries, multiple/included host names, per-host client CAs and CRLs,
passphrase-protected keys, and rejection of malformed pg_hosts.conf entries
(which must make startup fail).

Requires an OpenSSL build (SNI is unsupported on LibreSSL) and ``ssl`` in
PG_TEST_EXTRA.
"""

import pathlib
import platform
import subprocess

import pytest

from libpq import LibpqError
from pypg import check_pg_config, require_test_extras

pytestmark = require_test_extras("ssl")

SERVERHOSTADDR = "127.0.0.1"
SERVERHOSTCIDR = "127.0.0.1/32"


def test_sni(create_pg, ssl_server):
    if not check_pg_config("#define USE_OPENSSL 1"):
        pytest.skip("OpenSSL not supported by this build")
    # LibreSSL doesn't define HAVE_SSL_CTX_SET_CERT_CB.
    if not check_pg_config("#define HAVE_SSL_CTX_SET_CERT_CB 1"):
        pytest.skip("SNI not supported when building with LibreSSL")

    node = create_pg("primary", hostaddr=SERVERHOSTADDR)
    hosts_conf = pathlib.Path(node.datadir) / "pg_hosts.conf"

    exec_backend = node.sql("SHOW debug_exec_backend")

    ssl_server.configure_test_server_for_ssl(
        node, SERVERHOSTADDR, SERVERHOSTCIDR, "trust"
    )
    ssl_server.switch_server_cert(node, certfile="server-cn-only")

    base = dict(user="ssltestuser", dbname="trustdb", hostaddr=SERVERHOSTADDR, sslsni="1")

    def C(**kw):
        opts = dict(base)
        opts.update(kw)
        # The Perl connstr never sets `host`, relying on hostaddr only (no SNI
        # name). connect() would otherwise inject host=127.0.0.1, which sends no
        # SNI either (it's an IP), so the default is equivalent.
        opts.setdefault("host", SERVERHOSTADDR)
        return opts

    def connect_ok(opts, like=()):
        offset = node.current_log_position()
        with node.connect(**opts):
            pass
        for pat in like:
            node.wait_for_log(pat, offset)

    def connect_fails(opts, match):
        with pytest.raises(LibpqError, match=match):
            with node.connect(**opts):
                pass

    def try_restart():
        """Attempt a restart; return True if the server came back up."""
        try:
            node.pg_ctl("restart")
            return True
        except subprocess.CalledProcessError:
            return False

    root_server_ca = ssl_server.cert("root+server_ca.crt")
    root_ca = ssl_server.cert("root_ca.crt")

    # --- postgresql.conf ---

    # Connect without any hosts configured in pg_hosts.conf, thus using the
    # cert and key in postgresql.conf. pg_hosts.conf exists at this point but is
    # empty apart from the sample comments.
    connect_ok(C(sslrootcert=root_server_ca, sslmode="require"))
    connect_fails(
        C(sslrootcert=root_ca, sslmode="verify-ca"), r"certificate verify failed"
    )

    # Add an entry in pg_hosts.conf with no default, and reload. Since ssl_sni
    # is still 'off' we should still be able to connect using the certificates
    # in postgresql.conf.
    node.append_conf(
        "example.org server-cn-only.crt server-cn-only.key", filename="pg_hosts.conf"
    )
    node.pg_ctl("reload")
    connect_ok(C(sslrootcert=root_server_ca, sslmode="require"))

    # Turn on SNI support and remove pg_hosts.conf and reload to make sure a
    # missing file is treated like an empty file.
    node.append_conf("ssl_sni = on")
    hosts_conf.unlink()
    node.pg_ctl("reload")
    connect_ok(C(sslrootcert=root_server_ca, sslmode="require"))

    # --- pg_hosts.conf ---

    # Replicate the postgresql.conf configuration into pg_hosts.conf.
    node.append_conf("* server-cn-only.crt server-cn-only.key", filename="pg_hosts.conf")
    node.pg_ctl("reload")
    connect_ok(C(sslrootcert=root_server_ca, sslmode="require"))
    connect_fails(
        C(sslrootcert=root_ca, sslmode="verify-ca"), r"certificate verify failed"
    )

    # Add host entry for example.org serving the server cert and its
    # intermediate CA. The default host still exists without a CA.
    node.append_conf(
        "example.org server-cn-only+server_ca.crt server-cn-only.key root_ca.crt",
        filename="pg_hosts.conf",
    )
    node.pg_ctl("reload")
    connect_ok(C(host="example.org", sslrootcert=root_ca, sslmode="verify-ca"))
    connect_ok(C(host="Example.ORG", sslrootcert=root_ca, sslmode="verify-ca"))
    connect_fails(
        C(host="example.org", sslrootcert="invalid", sslmode="verify-ca"),
        r'root certificate file "invalid" does not exist',
    )
    connect_fails(
        C(sslrootcert=root_ca, sslmode="verify-ca"), r"certificate verify failed"
    )
    connect_ok(C(sslrootcert=root_server_ca, sslmode="require"))

    # Use multiple hostnames for a single configuration.
    hosts_conf.unlink()
    node.append_conf(
        "example.org,example.com,example.net server-cn-only+server_ca.crt "
        "server-cn-only.key root_ca.crt",
        filename="pg_hosts.conf",
    )
    node.pg_ctl("reload")
    connect_ok(C(host="example.org", sslrootcert=root_ca, sslmode="verify-ca"))
    connect_ok(C(host="example.com", sslrootcert=root_ca, sslmode="verify-ca"))
    connect_ok(C(host="example.net", sslrootcert=root_ca, sslmode="verify-ca"))
    connect_fails(
        C(sslrootcert=root_server_ca, sslmode="require", host="example.se"),
        r"unrecognized name",
    )

    # Test @-inclusion of hostnames.
    hosts_conf.unlink()
    node.append_conf(
        "example.org,@hostnames.txt server-cn-only+server_ca.crt "
        "server-cn-only.key root_ca.crt",
        filename="pg_hosts.conf",
    )
    node.append_conf("", "example.com", "example.net", filename="hostnames.txt")
    node.pg_ctl("reload")
    connect_ok(C(host="example.org", sslrootcert=root_ca, sslmode="verify-ca"))
    connect_ok(C(host="example.com", sslrootcert=root_ca, sslmode="verify-ca"))
    connect_ok(C(host="example.net", sslrootcert=root_ca, sslmode="verify-ca"))
    connect_fails(
        C(sslrootcert=root_server_ca, sslmode="require", host="example.se"),
        r"unrecognized name",
    )

    # A default entry combined with hostnames is invalid; startup must fail.
    hosts_conf.unlink()
    node.append_conf(
        "example.org,*,example.net server-cn-only+server_ca.crt "
        "server-cn-only.key root_ca.crt",
        filename="pg_hosts.conf",
    )
    assert not try_restart(), (
        "restart fails with default entry combined with hostnames"
    )

    # Duplicate entries are invalid.
    hosts_conf.unlink()
    node.append_conf(
        "* server-cn-only.crt server-cn-only.key",
        "* server-cn-only.crt server-cn-only.key",
        filename="pg_hosts.conf",
    )
    assert not try_restart(), "restart fails with two default entries"

    hosts_conf.unlink()
    node.append_conf(
        "/no_sni/ server-cn-only.crt server-cn-only.key",
        "/no_sni/ server-cn-only.crt server-cn-only.key",
        filename="pg_hosts.conf",
    )
    assert not try_restart(), "restart fails with two no_sni entries"

    hosts_conf.unlink()
    node.append_conf(
        "example.org server-cn-only.crt server-cn-only.key",
        "example.net server-cn-only.crt server-cn-only.key",
        "example.org server-cn-only.crt server-cn-only.key",
        filename="pg_hosts.conf",
    )
    assert not try_restart(), "restart fails with two identical hostname entries"

    hosts_conf.unlink()
    node.append_conf(
        "example.org server-cn-only.crt server-cn-only.key",
        "example.net,example.com,Example.org server-cn-only.crt server-cn-only.key",
        filename="pg_hosts.conf",
    )
    assert not try_restart(), (
        "restart fails with two identical hostname entries in lists"
    )

    # Modify pg_hosts.conf to no longer have the default host entry.
    hosts_conf.unlink()
    node.append_conf(
        "example.org server-cn-only+server_ca.crt server-cn-only.key root_ca.crt",
        filename="pg_hosts.conf",
    )
    node.pg_ctl("restart")

    # Connecting without a hostname, or with one not in pg_hosts, should fail.
    connect_fails(
        C(sslrootcert=root_server_ca, sslmode="require", sslsni="0"),
        r"handshake failure",
    )
    connect_fails(
        C(sslrootcert=root_server_ca, sslmode="require", host="example.com"),
        r"unrecognized name",
    )
    connect_fails(
        C(sslrootcert=root_server_ca, sslmode="require", host="example"),
        r"unrecognized name",
    )

    # Broken passphrase command for the key: the server should not start.
    hosts_conf.unlink()
    node.append_conf(
        'localhost server-cn-only.crt server-password.key root+client_ca.crt '
        '"echo wrongpassword" on',
        filename="pg_hosts.conf",
    )
    assert not try_restart(), (
        "restart fails with password-protected key when using the wrong passphrase"
    )

    # Correct passphrase set.
    hosts_conf.unlink()
    node.append_conf(
        'localhost server-cn-only.crt server-password.key root+client_ca.crt '
        '"echo secret1" on',
        filename="pg_hosts.conf",
    )
    assert try_restart(), (
        "restart succeeds with password-protected key and correct passphrase"
    )

    # Connecting works; stress the reload logic with subsequent reloads.
    connect_ok(C(sslrootcert=root_server_ca, sslmode="require", host="localhost"))
    node.pg_ctl("reload")
    node.pg_ctl("reload")
    connect_ok(C(sslrootcert=root_server_ca, sslmode="require", host="localhost"))
    node.pg_ctl("reload")
    node.pg_ctl("reload")
    connect_ok(C(sslrootcert=root_server_ca, sslmode="require", host="localhost"))

    # Reload a passphrase-protected key without reload support in the hook.
    # Restart should log no error, but a subsequent reload should fail with a
    # reload-specific error.
    hosts_conf.unlink()
    node.append_conf(
        'localhost server-cn-only.crt server-password.key root+client_ca.crt '
        '"echo secret1" off',
        filename="pg_hosts.conf",
    )
    offset = node.current_log_position()
    assert try_restart(), (
        "restart succeeds with password-protected key and correct passphrase"
    )
    assert "cannot be reloaded because it requires a passphrase" not in node.log_since(
        offset
    )

    # Passphrase reloads must be enabled on Windows (and EXEC_BACKEND) to
    # succeed even without a restart.
    if platform.system() != "Windows" and exec_backend != "on":
        connect_ok(C(sslrootcert=root_server_ca, sslmode="require", host="localhost"))
        # Reloading should fail since the passphrase cannot be reloaded, with an
        # error recorded in the log. Since we keep existing contexts around it
        # should still work.
        offset = node.current_log_position()
        node.pg_ctl("reload")
        connect_ok(C(sslrootcert=root_server_ca, sslmode="require", host="localhost"))
        assert "cannot be reloaded because it requires a passphrase" in node.log_since(
            offset
        )

    # Configure with only non-SNI connections allowed.
    hosts_conf.unlink()
    node.append_conf(
        "/no_sni/ server-cn-only.crt server-cn-only.key", filename="pg_hosts.conf"
    )
    node.pg_ctl("restart")
    connect_ok(C(sslrootcert=root_server_ca, sslmode="require", sslsni="0"))
    connect_fails(
        C(sslrootcert=root_server_ca, sslmode="require", host="example.org"),
        r"unrecognized name",
    )

    # --- Test client CAs ---

    hosts_conf.unlink()
    # Neither ssl_ca_file nor the default host should have any effect on the
    # following tests.
    node.append_conf("ssl_ca_file = 'root+client_ca.crt'")
    node.append_conf(
        "* server-cn-only.crt server-cn-only.key root+client_ca.crt",
        filename="pg_hosts.conf",
    )
    # example.org has an unconfigured CA.
    node.append_conf(
        "example.org server-cn-only.crt server-cn-only.key", filename="pg_hosts.conf"
    )
    # example.com uses the client CA.
    node.append_conf(
        "example.com server-cn-only.crt server-cn-only.key root+client_ca.crt",
        filename="pg_hosts.conf",
    )
    # example.net uses the server CA (which is wrong).
    node.append_conf(
        "example.net server-cn-only.crt server-cn-only.key root+server_ca.crt",
        filename="pg_hosts.conf",
    )
    node.pg_ctl("restart")

    base = dict(
        user="ssltestuser",
        dbname="certdb",
        hostaddr=SERVERHOSTADDR,
        sslmode="require",
        sslsni="1",
    )

    client_crt = ssl_server.cert("client.crt")
    client_key = ssl_server.sslkey("client.key")

    # example.org is unconfigured and should fail.
    connect_fails(
        C(host="example.org", sslcertmode="require", sslcert=client_crt, sslkey=client_key),
        r"client certificates can only be checked if a root certificate store "
        r"is available",
    )

    # example.com is configured and should require a valid client cert.
    connect_fails(
        C(host="example.com", sslcertmode="disable"),
        r"connection requires a valid client certificate",
    )
    connect_ok(
        C(host="example.com", sslcertmode="require", sslcert=client_crt, sslkey=client_key)
    )

    # example.net is configured and should require a client cert, but always
    # fails verification.
    connect_fails(
        C(host="example.net", sslcertmode="disable"),
        r"connection requires a valid client certificate",
    )
    connect_fails(
        C(host="example.net", sslcertmode="require", sslcert=client_crt, sslkey=client_key),
        r"unknown ca",
    )

    # Make sure the global CRL dir interacts properly with per-host trust.
    ssl_server.switch_server_cert(node, certfile="server-cn-only", crldir="client-crldir")
    connect_fails(
        C(
            host="example.com",
            sslcertmode="require",
            sslcert=ssl_server.cert("client-revoked.crt"),
            sslkey=ssl_server.sslkey("client-revoked.key"),
        ),
        r"certificate revoked",
    )

    # pg_hosts.conf with useless data at EOL.
    hosts_conf.unlink()
    node.append_conf(
        'example.org server-cn-only.crt server-cn-only.key root+client_ca.crt '
        '"cmd" on TRAILING_TEXT MORE_TEXT',
        filename="pg_hosts.conf",
    )
    assert not try_restart(), "restart fails with extra data at EOL"

    hosts_conf.unlink()
    node.append_conf(
        'example.org server-cn-only.crt server-cn-only.key root+client_ca.crt '
        '"cmd" notabooleanvalue',
        filename="pg_hosts.conf",
    )
    assert not try_restart(), "restart fails with non-boolean value in boolean field"
