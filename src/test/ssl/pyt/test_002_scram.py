# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/ssl/t/002_scram.pl.

Tests SCRAM authentication and TLS channel binding over SSL: basic SCRAM, the
``channel_binding`` connection option (invalid/disable/require), that channel
binding is unavailable for an MD5 password or for cert authentication, that it
composes with ``require_auth``, and that an RSA-PSS server certificate works.

Requires an OpenSSL build and ``ssl`` in PG_TEST_EXTRA (the SSL tests bind TCP,
so they are gated as potentially unsafe).
"""

import pytest

from libpq import LibpqError
from pypg import check_pg_config, require_test_extras

pytestmark = require_test_extras("ssl")

SERVERHOSTADDR = "127.0.0.1"
SERVERHOSTCIDR = "127.0.0.1/32"


def test_scram(create_pg, ssl_server):
    if not check_pg_config("#define USE_OPENSSL 1"):
        pytest.skip("OpenSSL not supported by this build")

    # LibreSSL doesn't define HAVE_SSL_CTX_SET_CERT_CB; and as of 5/2025 it
    # doesn't actually work for RSA-PSS certificates.
    libressl = not check_pg_config("#define HAVE_SSL_CTX_SET_CERT_CB 1")
    supports_rsapss_certs = (
        check_pg_config("#define HAVE_X509_GET_SIGNATURE_INFO 1") and not libressl
    )

    node = create_pg("primary", hostaddr=SERVERHOSTADDR)

    # could fail in FIPS mode
    try:
        node.sql("select md5('')")
        md5_works = True
    except LibpqError:
        md5_works = False

    ssl_server.configure_test_server_for_ssl(
        node,
        SERVERHOSTADDR,
        SERVERHOSTCIDR,
        "scram-sha-256",
        password="pass",
        password_enc="scram-sha-256",
    )
    ssl_server.switch_server_cert(node, certfile="server-cn-only")

    # Connection options shared among most tests, protecting against any
    # defaults in ~/.postgresql/.
    def common(**extra):
        opts = dict(
            dbname="trustdb",
            sslmode="require",
            sslcert="invalid",
            sslrootcert="invalid",
            hostaddr=SERVERHOSTADDR,
            host="localhost",
            password="pass",
        )
        opts.update(extra)
        return opts

    def connect_ok(like=(), **opts):
        offset = node.current_log_position()
        with node.connect(**opts):
            pass
        for pat in like:
            node.wait_for_log(pat, offset)

    def connect_fails(match, **opts):
        with pytest.raises(LibpqError, match=match):
            with node.connect(**opts):
                pass

    # Default settings.
    connect_ok(**common(user="ssltestuser"))

    # Test channel_binding.
    connect_fails(
        r'invalid channel_binding value: "invalid_value"',
        **common(user="ssltestuser", channel_binding="invalid_value"),
    )
    connect_ok(**common(user="ssltestuser", channel_binding="disable"))
    connect_ok(**common(user="ssltestuser", channel_binding="require"))

    # Now test when the user has an MD5-encrypted password; should fail.
    if md5_works:
        connect_fails(
            r"channel binding required but not supported by server's "
            r"authentication request",
            **common(user="md5testuser", channel_binding="require"),
        )

    # Now test with auth method 'cert' by connecting to 'certdb'. Should fail,
    # because channel binding is not performed.
    cert_opts = dict(
        sslcert=ssl_server.cert("client.crt"),
        sslkey=ssl_server.sslkey("client.key"),
        sslrootcert="invalid",
        hostaddr=SERVERHOSTADDR,
        host="localhost",
    )
    connect_fails(
        r"channel binding required, but server authenticated client without "
        r"channel binding",
        **cert_opts,
        dbname="certdb",
        user="ssltestuser",
        channel_binding="require",
    )

    # Certificate verification at the connection level should still work fine.
    connect_ok(
        like=[
            r'connection authenticated: identity="ssltestuser" method=scram-sha-256'
        ],
        **cert_opts,
        dbname="verifydb",
        user="ssltestuser",
        password="pass",
    )

    # channel_binding should continue to work independently of require_auth.
    connect_ok(
        **common(
            user="ssltestuser",
            channel_binding="disable",
            require_auth="scram-sha-256",
        )
    )
    if md5_works:
        connect_fails(
            r"channel binding required but not supported by server's "
            r"authentication request",
            **common(
                user="md5testuser",
                require_auth="md5",
                channel_binding="require",
            ),
        )
    connect_ok(
        **common(
            user="ssltestuser",
            channel_binding="require",
            require_auth="scram-sha-256",
        )
    )

    # Now test with a server certificate that uses the RSA-PSS algorithm. This
    # checks that the certificate can be loaded and that channel binding works
    # (see bug #17760).
    if supports_rsapss_certs:
        ssl_server.switch_server_cert(node, certfile="server-rsapss")
        connect_ok(
            like=[
                r'connection authenticated: identity="ssltestuser" '
                r"method=scram-sha-256"
            ],
            **common(user="ssltestuser", channel_binding="require"),
        )
