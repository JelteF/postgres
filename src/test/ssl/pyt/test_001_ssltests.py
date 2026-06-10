# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/ssl/t/001_ssltests.pl.

The main SSL test matrix. It covers passphrase-protected server keys (with and
without reload support), SSL protocol-version bounds and group parsing, the
client-side sslmode/sslrootcert/sslcrl behaviour, server certificate host-name
matching (CN, SANs, IP addresses, wildcards, system trust store), and
server-side certificate authentication (client certs in PEM/DER, encrypted
keys, sslcertmode, DN/CN ident maps, CRLs and revoked certs, intermediate CAs).

Requires an OpenSSL build and ``ssl`` in PG_TEST_EXTRA.

Differences from the Perl original:
 - connect_ok/connect_fails are `node.connect()` returning or raising
   `LibpqError`; expected client error text is matched on the exception and
   `log_like`/`log_unlike` against the server log.
 - The non-fatal `sslkeylogfile` "could not open" warning is libpq client-side
   stderr (not captured by the wrapper), so only the successful connection is
   asserted there.
 - The pg_stat_ssl client_serial is matched as `\\d+` (the fallback the Perl
   uses when the openssl CLI isn't available) rather than computing the exact
   serial.
 - The pty-dependent TODO block (empty/no password on an encrypted key) is
   omitted, as in the original.
"""

import os
import platform
import re
import subprocess

import pytest

from libpq import LibpqError
from pypg import check_pg_config, require_test_extras

pytestmark = require_test_extras("ssl")

SERVERHOSTADDR = "127.0.0.1"
SERVERHOSTCIDR = "127.0.0.1/32"

DEFAULT_SSL = dict(
    sslkey="invalid",
    sslcert="invalid",
    sslrootcert="invalid",
    sslcrl="invalid",
    sslcrldir="invalid",
)


def test_ssltests(create_pg, ssl_server, pg_bin, tmp_path, monkeypatch):
    if not check_pg_config("#define USE_OPENSSL 1"):
        pytest.skip("OpenSSL not supported by this build")

    libressl = not check_pg_config("#define HAVE_SSL_CTX_SET_CERT_CB 1")
    supports_sslcertmode_require = check_pg_config(
        "#define HAVE_SSL_CTX_SET_CERT_CB 1"
    )
    have_inet_pton = check_pg_config("#define HAVE_INET_PTON 1")

    def cert(name):
        return ssl_server.cert(name)

    def key(name):
        return ssl_server.sslkey(name)

    node = create_pg(
        "primary",
        hostaddr=SERVERHOSTADDR,
        # Needed to allow inspecting the postmaster log on failed connections.
        conf=["log_min_messages = debug2"],
    )

    # Run this before we lock down access below.
    assert node.sql("SHOW ssl_library") == "OpenSSL", "ssl_library parameter"
    exec_backend = node.sql("SHOW debug_exec_backend")

    ssl_server.configure_test_server_for_ssl(
        node, SERVERHOSTADDR, SERVERHOSTCIDR, "trust"
    )

    # base is reassigned as the shared connection options change through the
    # test; C() merges it with per-call overrides.
    base = {}

    def C(**kw):
        opts = dict(base)
        opts.update(kw)
        return opts

    def connect_ok(opts, like=()):
        offset = node.current_log_position()
        with node.connect(**opts):
            pass
        for pat in like:
            node.wait_for_log(pat, offset)

    def connect_fails(opts, match=None, like=(), unlike=()):
        offset = node.current_log_position()
        ctx = pytest.raises(LibpqError, match=match) if match else pytest.raises(LibpqError)
        with ctx:
            with node.connect(**opts):
                pass
        for pat in like:
            node.wait_for_log(pat, offset)
        log = node.log_since(offset)
        for pat in unlike:
            assert not re.search(pat, log), f"unexpected {pat!r} in log"

    def try_restart():
        try:
            node.pg_ctl("restart")
            return True
        except subprocess.CalledProcessError:
            return False

    root_server_ca = cert("root+server_ca.crt")

    base = dict(
        DEFAULT_SSL,
        user="ssltestuser",
        dbname="trustdb",
        hostaddr=SERVERHOSTADDR,
        host="common-name.pg-ssltest.test",
    )

    # --- password-protected keys ---

    # A passphrase command which fails to unlock the key: server must not start.
    ssl_server.switch_server_cert(
        node,
        certfile="server-cn-only",
        cafile="root+client_ca",
        keyfile="server-password",
        passphrase_cmd="echo wrongpassword",
        restart=False,
    )
    offset = node.current_log_position()
    assert not try_restart(), (
        "restart fails with password-protected key file with wrong password"
    )
    assert "could not load private key file" in node.log_since(offset)

    # A passphrase command which unlocks the key but doesn't support reloading.
    ssl_server.switch_server_cert(
        node,
        certfile="server-cn-only",
        cafile="root+client_ca",
        keyfile="server-password",
        passphrase_cmd="echo secret1",
        passphrase_cmd_reload="off",
        restart=False,
    )
    offset = node.current_log_position()
    assert try_restart(), "restart succeeds with password-protected key file"
    assert "could not load private key file" not in node.log_since(offset)

    if exec_backend == "on":
        connect_fails(
            C(sslrootcert=root_server_ca, sslmode="require"),
            match=r"server does not support SSL",
        )
    else:
        connect_ok(C(sslrootcert=root_server_ca, sslmode="require"))

    # Reloading should fail since we cannot execute the passphrase command.
    node.pg_ctl("reload")
    node.wait_for_log(r"cannot be reloaded because it requires a passphrase")

    # A passphrase command that unlocks the key and supports reloading.
    ssl_server.switch_server_cert(
        node,
        certfile="server-cn-only",
        cafile="root+client_ca",
        keyfile="server-password",
        passphrase_cmd="echo secret1",
        passphrase_cmd_reload="on",
        restart=False,
    )
    offset = node.current_log_position()
    assert try_restart(), "restart succeeds with password-protected key file"
    assert "could not load private key file" not in node.log_since(offset)
    connect_ok(C(sslrootcert=root_server_ca, sslmode="require"))

    # Reloading executes the passphrase reload command and reloads the key.
    offset = node.current_log_position()
    node.pg_ctl("reload")
    node.wait_for_log(r"reloading configuration files", offset)
    assert (
        "cannot be reloaded because it requires a passphrase"
        not in node.log_since(offset)
    ), "passphrase could reload private key"
    connect_ok(C(sslrootcert=root_server_ca, sslmode="require"))

    # --- SSL protocol bounds ---
    # TLSv1.1 is lower than TLSv1.2, so it won't work.
    node.append_conf(
        "ssl_min_protocol_version='TLSv1.2'", "ssl_max_protocol_version='TLSv1.1'"
    )
    assert not try_restart(), "restart fails with incorrect SSL protocol bounds"
    # Go back to the defaults, this works.
    node.append_conf(
        "ssl_min_protocol_version='TLSv1.2'", "ssl_max_protocol_version=''"
    )
    assert try_restart(), "restart succeeds with correct SSL protocol bounds"

    # Colon-separated groups parsing. switch_server_cert below will overwrite
    # ssl_groups with a known set, so a bad value here is fine to leave.
    node.append_conf("ssl_groups='bad:value'", filename="sslconfig.conf")
    offset = node.current_log_position()
    assert not try_restart(), "restart fails with incorrect groups"
    assert "no SSL error reported" not in node.log_since(offset), (
        "error message translated"
    )

    # --- client-side tests (no client certificate) ---
    ssl_server.switch_server_cert(node, certfile="server-cn-only")

    if not libressl:
        # Connect should work with a given sslkeylogfile.
        keylog = tmp_path / "key.txt"
        connect_ok(C(sslrootcert=root_server_ca, sslkeylogfile=str(keylog), sslmode="require"))
        assert keylog.is_file(), f"keylog file exists at {keylog}"
        if platform.system() != "Windows":
            assert (os.stat(keylog).st_mode & 0o006) == 0, (
                "keylog file is not world readable"
            )
        # An incorrect sslkeylogfile path prints a (non-fatal) error to client
        # stderr but the connection still succeeds.
        connect_ok(
            C(
                sslrootcert=root_server_ca,
                sslkeylogfile=str(tmp_path / "invalid" / "key.txt"),
                sslmode="require",
            )
        )

    # The server should not accept non-SSL connections.
    connect_fails(C(sslmode="disable"), match=r"no pg_hba\.conf entry")

    # Without a root cert: require works; verify-ca/full fail.
    connect_ok(C(sslrootcert="invalid", sslmode="require"))
    connect_fails(
        C(sslrootcert="invalid", sslmode="verify-ca"),
        match=r'root certificate file "invalid" does not exist',
    )
    connect_fails(
        C(sslrootcert="invalid", sslmode="verify-full"),
        match=r'root certificate file "invalid" does not exist',
    )

    # Wrong root cert (client CA instead of server CA): fail.
    for mode in ("require", "verify-ca", "verify-full"):
        connect_fails(
            C(sslrootcert=cert("client_ca.crt"), sslmode=mode),
            match=r"SSL error: certificate verify failed",
        )

    # Just the server CA's cert, without the root: fail.
    connect_fails(
        C(sslrootcert=cert("server_ca.crt"), sslmode="verify-ca"),
        match=r"SSL error: certificate verify failed",
    )

    # Correct root cert.
    for mode in ("require", "verify-ca", "verify-full"):
        connect_ok(C(sslrootcert=root_server_ca, sslmode=mode))

    # Root file with two certificates, either order.
    connect_ok(C(sslrootcert=cert("both-cas-1.crt"), sslmode="verify-ca"))
    connect_ok(C(sslrootcert=cert("both-cas-2.crt"), sslmode="verify-ca"))

    # sslcertmode=allow/disable work without a client certificate.
    connect_ok(C(sslrootcert=root_server_ca, sslmode="require", sslcertmode="disable"))
    connect_ok(C(sslrootcert=root_server_ca, sslmode="require", sslcertmode="allow"))
    # sslcertmode=require should fail without a client certificate.
    connect_fails(
        C(sslrootcert=root_server_ca, sslmode="require", sslcertmode="require"),
        match=(
            r"server accepted connection without a valid SSL certificate"
            if supports_sslcertmode_require
            else r'sslcertmode value "require" is not supported'
        ),
    )

    # CRL tests.
    connect_ok(C(sslrootcert=root_server_ca, sslmode="verify-ca", sslcrl="invalid"))
    connect_fails(
        C(sslrootcert=root_server_ca, sslmode="verify-ca", sslcrl=cert("client.crl")),
        match=r"SSL error: certificate verify failed",
    )
    connect_fails(
        C(
            sslcrl="",
            sslrootcert=root_server_ca,
            sslmode="verify-ca",
            sslcrldir=cert("client-crldir"),
        ),
        match=r"SSL error: certificate verify failed",
    )
    connect_ok(
        C(sslrootcert=root_server_ca, sslmode="verify-ca", sslcrl=cert("root+server.crl"))
    )
    connect_ok(
        C(sslrootcert=root_server_ca, sslmode="verify-ca", sslcrldir=cert("root+server-crldir"))
    )

    # Host name vs. server certificate.
    base = dict(
        DEFAULT_SSL,
        user="ssltestuser",
        dbname="trustdb",
        sslrootcert=root_server_ca,
        hostaddr=SERVERHOSTADDR,
    )
    connect_ok(C(sslmode="require", host="wronghost.test"))
    connect_ok(C(sslmode="verify-ca", host="wronghost.test"))
    connect_fails(
        C(sslmode="verify-full", host="wronghost.test"),
        match=r'server certificate for "common-name\.pg-ssltest\.test" does not '
        r'match host name "wronghost\.test"',
    )

    # IP address in the Common Name.
    ssl_server.switch_server_cert(node, certfile="server-ip-cn-only")
    base = dict(
        DEFAULT_SSL,
        user="ssltestuser",
        dbname="trustdb",
        sslrootcert=root_server_ca,
        hostaddr=SERVERHOSTADDR,
        sslmode="verify-full",
    )
    connect_ok(C(host="192.0.2.1", sslsni="0"))
    connect_fails(
        C(host="192.000.002.001", sslsni="0"),
        match=r'server certificate for "192\.0\.2\.1" does not match host name '
        r'"192\.000\.002\.001"',
    )

    # IP address in a dNSName SAN.
    ssl_server.switch_server_cert(node, certfile="server-ip-in-dnsname")
    connect_ok(C(host="192.0.2.1", sslsni="0"))

    # Subject Alternative Names.
    ssl_server.switch_server_cert(node, certfile="server-multiple-alt-names")
    base = dict(
        DEFAULT_SSL,
        user="ssltestuser",
        dbname="trustdb",
        sslrootcert=root_server_ca,
        hostaddr=SERVERHOSTADDR,
        sslmode="verify-full",
    )
    connect_ok(C(host="dns1.alt-name.pg-ssltest.test"))
    connect_ok(C(host="dns2.alt-name.pg-ssltest.test"))
    connect_ok(C(host="foo.wildcard.pg-ssltest.test"))
    connect_fails(
        C(host="wronghost.alt-name.pg-ssltest.test"),
        match=r'server certificate for "dns1\.alt-name\.pg-ssltest\.test" '
        r'\(and 2 other names\) does not match host name '
        r'"wronghost\.alt-name\.pg-ssltest\.test"',
    )
    connect_fails(
        C(host="deep.subdomain.wildcard.pg-ssltest.test"),
        match=r'server certificate for "dns1\.alt-name\.pg-ssltest\.test" '
        r'\(and 2 other names\) does not match host name '
        r'"deep\.subdomain\.wildcard\.pg-ssltest\.test"',
    )

    # Single Subject Alternative Name (slightly different error message).
    ssl_server.switch_server_cert(node, certfile="server-single-alt-name")
    base = dict(
        DEFAULT_SSL,
        user="ssltestuser",
        dbname="trustdb",
        sslrootcert=root_server_ca,
        hostaddr=SERVERHOSTADDR,
        sslmode="verify-full",
    )
    connect_ok(C(host="single.alt-name.pg-ssltest.test"))
    connect_fails(
        C(host="wronghost.alt-name.pg-ssltest.test"),
        match=r'server certificate for "single\.alt-name\.pg-ssltest\.test" '
        r'does not match host name "wronghost\.alt-name\.pg-ssltest\.test"',
    )
    connect_fails(
        C(host="deep.subdomain.wildcard.pg-ssltest.test"),
        match=r'server certificate for "single\.alt-name\.pg-ssltest\.test" '
        r'does not match host name "deep\.subdomain\.wildcard\.pg-ssltest\.test"',
    )

    if have_inet_pton:
        # Certificate with IP addresses in the SANs.
        ssl_server.switch_server_cert(node, certfile="server-ip-alt-names")
        connect_ok(C(host="192.0.2.1"))
        connect_ok(C(host="192.000.002.001"))
        connect_fails(
            C(host="192.0.2.2"),
            match=r'server certificate for "192\.0\.2\.1" \(and 1 other name\) '
            r'does not match host name "192\.0\.2\.2"',
        )
        connect_ok(C(host="2001:DB8::1"))
        connect_ok(C(host="2001:db8:0:0:0:0:0:1"))
        connect_ok(C(host="2001:db8::0.0.0.1"))
        connect_fails(
            C(host="::1"),
            match=r'server certificate for "192\.0\.2\.1" \(and 1 other name\) '
            r'does not match host name "::1"',
        )
        connect_fails(
            C(host="2001:DB8::1/128"),
            match=r'server certificate for "192\.0\.2\.1" \(and 1 other name\) '
            r'does not match host name "2001:DB8::1/128"',
        )

    # CN + DNS SANs: the CN should be ignored.
    ssl_server.switch_server_cert(node, certfile="server-cn-and-alt-names")
    base = dict(
        DEFAULT_SSL,
        user="ssltestuser",
        dbname="trustdb",
        sslrootcert=root_server_ca,
        hostaddr=SERVERHOSTADDR,
        sslmode="verify-full",
    )
    connect_ok(C(host="dns1.alt-name.pg-ssltest.test"))
    connect_ok(C(host="dns2.alt-name.pg-ssltest.test"))
    connect_fails(
        C(host="common-name.pg-ssltest.test"),
        match=r'server certificate for "dns1\.alt-name\.pg-ssltest\.test" '
        r'\(and 1 other name\) does not match host name '
        r'"common-name\.pg-ssltest\.test"',
    )

    if have_inet_pton:
        # Fall back to the CN if the SANs contain only IP addresses.
        ssl_server.switch_server_cert(node, certfile="server-cn-and-ip-alt-names")
        connect_ok(C(host="common-name.pg-ssltest.test"))
        connect_ok(C(host="192.0.2.1"))
        connect_ok(C(host="2001:db8::1"))

        # Same, with IP addresses and DNS names swapped.
        ssl_server.switch_server_cert(node, certfile="server-ip-cn-and-alt-names")
        connect_ok(C(host="192.0.2.2"))
        connect_ok(C(host="2001:db8::1"))
        connect_fails(
            C(host="192.0.2.1"),
            match=r'server certificate for "192\.0\.2\.2" \(and 1 other name\) '
            r'does not match host name "192\.0\.2\.1"',
        )

    ssl_server.switch_server_cert(node, certfile="server-ip-cn-and-dns-alt-names")
    connect_ok(C(host="192.0.2.1"))
    connect_ok(C(host="dns1.alt-name.pg-ssltest.test"))
    connect_ok(C(host="dns2.alt-name.pg-ssltest.test"))

    # Certificate with no CN or SANs.
    ssl_server.switch_server_cert(node, certfile="server-no-names")
    base = dict(
        DEFAULT_SSL,
        user="ssltestuser",
        dbname="trustdb",
        sslrootcert=root_server_ca,
        hostaddr=SERVERHOSTADDR,
    )
    connect_ok(C(sslmode="verify-ca", host="common-name.pg-ssltest.test"))
    connect_fails(
        C(sslmode="verify-full", host="common-name.pg-ssltest.test"),
        match=r"could not get server's host name from server certificate",
    )

    # System trusted roots.
    ssl_server.switch_server_cert(
        node, certfile="server-cn-only+server_ca", keyfile="server-cn-only", cafile="root_ca"
    )
    base = dict(
        DEFAULT_SSL,
        user="ssltestuser",
        dbname="trustdb",
        sslrootcert="system",
        hostaddr=SERVERHOSTADDR,
    )
    # By default our custom-CA-signed cert should not be trusted.
    connect_fails(
        C(sslmode="verify-full", host="common-name.pg-ssltest.test"),
        match=r"SSL error: (certificate verify failed|unregistered scheme)",
    )
    # Modes other than verify-full cannot be mixed with sslrootcert=system.
    connect_fails(
        C(sslmode="verify-ca", host="common-name.pg-ssltest.test"),
        match=r'weak sslmode "verify-ca" may not be used with sslrootcert=system',
    )

    if not libressl:
        # Redefine "system" so the cert is trusted again.
        monkeypatch.setenv("SSL_CERT_FILE", str(node.datadir / "root_ca.crt"))
        connect_ok(C(sslmode="verify-full", host="common-name.pg-ssltest.test"))
        # verify-full is the default for system CAs.
        connect_fails(
            C(host="common-name.pg-ssltest.test.bad"),
            match=r'server certificate for "common-name\.pg-ssltest\.test" does '
            r'not match host name "common-name\.pg-ssltest\.test\.bad"',
        )
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)

    # CRL.
    ssl_server.switch_server_cert(node, certfile="server-revoked")
    base = dict(
        DEFAULT_SSL,
        user="ssltestuser",
        dbname="trustdb",
        hostaddr=SERVERHOSTADDR,
        host="common-name.pg-ssltest.test",
    )
    connect_ok(C(sslrootcert=root_server_ca, sslmode="verify-ca"))
    connect_fails(
        C(sslrootcert=root_server_ca, sslmode="verify-ca", sslcrl=cert("root+server.crl")),
        match=r"SSL error: certificate verify failed",
    )
    connect_fails(
        C(
            sslcrl="",
            sslrootcert=root_server_ca,
            sslmode="verify-ca",
            sslcrldir=cert("root+server-crldir"),
        ),
        match=r"SSL error: certificate verify failed",
    )

    # pg_stat_ssl without a client certificate.
    def pg_stat_ssl(connstr, pattern):
        r = pg_bin.run(
            "psql",
            "--no-psqlrc",
            "--no-align",
            "--field-separator", ",",
            "--pset", "null=_null_",
            "--dbname", connstr,
            "--command", "SELECT * FROM pg_stat_ssl WHERE pid = pg_backend_pid()",
            server=node,
            check=True,
        )
        assert re.search(pattern, r.stdout, re.M), r.stdout

    common = (
        "sslkey=invalid sslcert=invalid sslrootcert=invalid sslcrl=invalid "
        "sslcrldir=invalid user=ssltestuser dbname=trustdb "
        f"hostaddr={SERVERHOSTADDR} host=common-name.pg-ssltest.test"
    )
    pg_stat_ssl(
        common + " sslrootcert=invalid",
        r"^pid,ssl,version,cipher,bits,client_dn,client_serial,issuer_dn\r?\n"
        r"^\d+,t,TLSv[\d.]+,[\w-]+,\d+,_null_,_null_,_null_\r?$",
    )

    # Min/max SSL protocol versions.
    connect_ok(
        C(
            sslrootcert=root_server_ca,
            sslmode="require",
            ssl_min_protocol_version="TLSv1.2",
            ssl_max_protocol_version="TLSv1.2",
        )
    )
    connect_fails(
        C(
            sslrootcert=root_server_ca,
            sslmode="require",
            ssl_min_protocol_version="TLSv1.2",
            ssl_max_protocol_version="TLSv1.1",
        ),
        match=r"invalid SSL protocol version range",
    )
    connect_fails(
        C(
            sslrootcert=root_server_ca,
            sslmode="require",
            ssl_min_protocol_version="incorrect_tls",
        ),
        match=r'invalid "ssl_min_protocol_version" value',
    )
    connect_fails(
        C(
            sslrootcert=root_server_ca,
            sslmode="require",
            ssl_max_protocol_version="incorrect_tls",
        ),
        match=r'invalid "ssl_max_protocol_version" value',
    )

    # --- server-side certificate authorization ---
    base = dict(
        DEFAULT_SSL,
        sslrootcert=root_server_ca,
        sslmode="require",
        dbname="certdb",
        hostaddr=SERVERHOSTADDR,
        host="localhost",
    )

    # no client cert
    connect_fails(
        C(user="ssltestuser", sslcert="invalid"),
        match=r"connection requires a valid client certificate",
    )
    # correct client cert in unencrypted PEM
    connect_ok(C(user="ssltestuser", sslcert=cert("client.crt"), sslkey=key("client.key")))
    # correct client cert in unencrypted DER
    connect_ok(
        C(user="ssltestuser", sslcert=cert("client.crt"), sslkey=key("client-der.key"))
    )
    # correct client cert in encrypted PEM
    connect_ok(
        C(
            user="ssltestuser",
            sslcert=cert("client.crt"),
            sslkey=key("client-encrypted-pem.key"),
            sslpassword="dUmmyP^#+",
        )
    )
    # correct client cert in encrypted DER
    connect_ok(
        C(
            user="ssltestuser",
            sslcert=cert("client.crt"),
            sslkey=key("client-encrypted-der.key"),
            sslpassword="dUmmyP^#+",
        )
    )
    # correct client cert with sslcertmode=require/allow
    if supports_sslcertmode_require:
        connect_ok(
            C(
                user="ssltestuser",
                sslcertmode="require",
                sslcert=cert("client.crt"),
                sslkey=key("client.key"),
            )
        )
    connect_ok(
        C(
            user="ssltestuser",
            sslcertmode="allow",
            sslcert=cert("client.crt"),
            sslkey=key("client.key"),
        )
    )
    # client cert not sent if sslcertmode=disable
    connect_fails(
        C(
            user="ssltestuser",
            sslcertmode="disable",
            sslcert=cert("client.crt"),
            sslkey=key("client.key"),
        ),
        match=r"connection requires a valid client certificate",
    )
    # encrypted PEM with wrong password
    connect_fails(
        C(
            user="ssltestuser",
            sslcert=cert("client.crt"),
            sslkey=key("client-encrypted-pem.key"),
            sslpassword="wrong",
        ),
        match=r'private key file ".*client-encrypted-pem\.key": bad decrypt',
    )

    # correct client cert using whole DN
    connect_ok(
        C(
            dbname="certdb_dn",
            user="ssltestuser",
            sslcert=cert("client-dn.crt"),
            sslkey=key("client-dn.key"),
        ),
        like=[
            r'connection authenticated: identity="CN=ssltestuser-dn,OU=Testing,'
            r'OU=Engineering,O=PGDG" method=cert'
        ],
    )
    # same with a regex
    connect_ok(
        C(
            dbname="certdb_dn_re",
            user="ssltestuser",
            sslcert=cert("client-dn.crt"),
            sslkey=key("client-dn.key"),
        )
    )
    # same using explicit CN
    connect_ok(
        C(
            dbname="certdb_cn",
            user="ssltestuser",
            sslcert=cert("client-dn.crt"),
            sslkey=key("client-dn.key"),
        ),
        like=[
            r'connection authenticated: identity="CN=ssltestuser-dn,OU=Testing,'
            r'OU=Engineering,O=PGDG" method=cert'
        ],
    )

    # pg_stat_ssl with a client certificate (serial matched generically).
    pg_stat_ssl(
        common + f" user=ssltestuser sslcert={cert('client.crt')} sslkey={key('client.key')}",
        r"^pid,ssl,version,cipher,bits,client_dn,client_serial,issuer_dn\r?\n"
        r"^\d+,t,TLSv[\d.]+,[\w-]+,\d+,/?CN=ssltestuser,\d+,/?"
        r"CN=Test CA for PostgreSQL SSL regression test client certs\r?$",
    )

    # client key with wrong permissions (skipped on Windows).
    if platform.system() != "Windows":
        connect_fails(
            C(user="ssltestuser", sslcert=cert("client.crt"), sslkey=key("client_wrongperms.key")),
            match=r'private key file ".*client_wrongperms\.key" has group or '
            r"world access",
        )

    # client cert belonging to another user
    connect_fails(
        C(user="anotheruser", sslcert=cert("client.crt"), sslkey=key("client.key")),
        match=r'certificate authentication failed for user "anotheruser"',
        like=[r'connection authenticated: identity="CN=ssltestuser" method=cert'],
    )

    # revoked client cert
    connect_fails(
        C(user="ssltestuser", sslcert=cert("client-revoked.crt"), sslkey=key("client-revoked.key")),
        match=r"SSL error: (ssl[a-z0-9/]*|tls) alert certificate revoked",
        like=[
            r"Client certificate verification failed at depth 0: certificate revoked",
            r'Failed certificate data \(unverified\): subject "/CN=ssltestuser", '
            r'serial number \d+, issuer "/CN=Test CA for PostgreSQL SSL '
            r'regression test client certs"',
        ],
        unlike=[r"connection authenticated:"],
    )

    # clientcert=verify-full: works iff username matches the Common Name.
    base = dict(
        DEFAULT_SSL,
        sslrootcert=root_server_ca,
        sslmode="require",
        dbname="verifydb",
        hostaddr=SERVERHOSTADDR,
        host="localhost",
    )
    connect_ok(
        C(user="ssltestuser", sslcert=cert("client.crt"), sslkey=key("client.key")),
        like=[r'connection authenticated: user="ssltestuser" method=trust'],
    )
    connect_fails(
        C(user="anotheruser", sslcert=cert("client.crt"), sslkey=key("client.key")),
        match=r'FATAL: .* "trust" authentication failed for user "anotheruser"',
        unlike=[r"connection authenticated:"],
    )
    # clientcert=verify-ca: works even when username doesn't match the CN.
    connect_ok(
        C(user="yetanotheruser", sslcert=cert("client.crt"), sslkey=key("client.key")),
        like=[r'connection authenticated: user="yetanotheruser" method=trust'],
    )

    # intermediate client_ca.crt provided by client, not in server ssl_ca_file.
    ssl_server.switch_server_cert(node, certfile="server-cn-only", cafile="root_ca")
    base = dict(
        DEFAULT_SSL,
        user="ssltestuser",
        dbname="certdb",
        sslkey=key("client.key"),
        sslrootcert=root_server_ca,
        hostaddr=SERVERHOSTADDR,
        host="localhost",
    )
    connect_ok(C(sslmode="require", sslcert=cert("client+client_ca.crt")))
    connect_fails(
        C(sslmode="require", sslcert=cert("client.crt")),
        match=r"SSL error: tlsv1 alert unknown ca",
        like=[
            r"Client certificate verification failed at depth 0: unable to get "
            r"local issuer certificate",
            r'Failed certificate data \(unverified\): subject "/CN=ssltestuser", '
            r'serial number \d+, issuer "/CN=Test CA for PostgreSQL SSL '
            r'regression test client certs"',
        ],
    )
    connect_fails(
        C(sslmode="require", sslcert=cert("client-long.crt"), sslkey=key("client-long.key")),
        match=r"SSL error: tlsv1 alert unknown ca",
        like=[
            r"Client certificate verification failed at depth 0: unable to get "
            r"local issuer certificate",
            r'Failed certificate data \(unverified\): subject "\.\.\./CN=ssl-'
            r'123456789012345678901234567890123456789012345678901234567890", '
            r'serial number \d+, issuer "/CN=Test CA for PostgreSQL SSL '
            r'regression test client certs"',
        ],
    )

    # Invalid cafile so the next test can't verify the client CA.
    ssl_server.switch_server_cert(node, certfile="server-cn-only", cafile="server-cn-only")
    # intermediate CA provided but without a trusted root (depth > 0 logging).
    connect_fails(
        C(sslmode="require", sslcert=cert("client+client_ca.crt")),
        match=r"SSL error: tlsv1 alert unknown ca",
        like=[
            r"Client certificate verification failed at depth 1: unable to get "
            r"local issuer certificate",
            (
                r'Failed certificate data \(unverified\): subject "/CN=Test CA '
                r'for PostgreSQL SSL regression test client certs", serial number '
                r'\d+, issuer "/CN=Test root CA for PostgreSQL SSL regression test '
                r'suite"'
            )
            if not libressl
            else (
                r'Failed certificate data \(unverified\): subject "/CN=ssltestuser",'
                r' serial number \d+, issuer "/CN=Test CA for PostgreSQL SSL '
                r'regression test client certs"'
            ),
        ],
    )

    # server-side CRL directory.
    ssl_server.switch_server_cert(node, certfile="server-cn-only", crldir="root+client-crldir")
    connect_fails(
        C(user="ssltestuser", sslcert=cert("client-revoked.crt"), sslkey=key("client-revoked.key")),
        match=r"SSL error: (ssl[a-z0-9/]*|tls) alert certificate revoked",
        like=[
            r"Client certificate verification failed at depth 0: certificate revoked",
            r'Failed certificate data \(unverified\): subject "/CN=ssltestuser", '
            r'serial number \d+, issuer "/CN=Test CA for PostgreSQL SSL '
            r'regression test client certs"',
        ],
    )
    # revoked client cert, non-ASCII subject.
    connect_fails(
        C(
            user="ssltestuser",
            sslcert=cert("client-revoked-utf8.crt"),
            sslkey=key("client-revoked-utf8.key"),
        ),
        match=r"SSL error: (ssl[a-z0-9/]*|tls) alert certificate revoked",
        like=[
            r"Client certificate verification failed at depth 0: certificate revoked",
            r'Failed certificate data \(unverified\): subject '
            r'"/CN=\\xce\\x9f\\xce\\xb4\\xcf\\x85\\xcf\\x83\\xcf\\x83\\xce\\xad'
            r'\\xce\\xb1\\xcf\\x82", serial number \d+, issuer "/CN=Test CA for '
            r'PostgreSQL SSL regression test client certs"',
        ],
    )

    # Per-host client CAs (requires sslcertmode=require support).
    if supports_sslcertmode_require:
        base = dict(
            user="ssltestuser",
            dbname="certdb",
            hostaddr=SERVERHOSTADDR,
            sslmode="require",
            sslsni="1",
        )
        ssl_server.switch_server_cert(node, certfile="server-cn-only", cafile="")
        connect_fails(
            C(host="example.org", sslcertmode="require", sslcert=cert("client.crt"), sslkey=key("client.key")),
            match=r"client certificates can only be checked if a root certificate "
            r"store is available",
        )

        ssl_server.switch_server_cert(node, certfile="server-cn-only", cafile="root+client_ca")
        connect_fails(
            C(host="example.com", sslcertmode="disable"),
            match=r"connection requires a valid client certificate",
        )
        connect_ok(
            C(host="example.com", sslcertmode="require", sslcert=cert("client.crt"), sslkey=key("client.key"))
        )

        ssl_server.switch_server_cert(node, certfile="server-cn-only", cafile="root+server_ca")
        connect_fails(
            C(host="example.net", sslcertmode="disable"),
            match=r"connection requires a valid client certificate",
        )
        connect_fails(
            C(host="example.net", sslcertmode="require", sslcert=cert("client.crt"), sslkey=key("client.key")),
            match=r"unknown ca",
        )
