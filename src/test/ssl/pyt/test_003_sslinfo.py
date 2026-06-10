# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/ssl/t/003_sslinfo.pl.

Exercises the ``sslinfo`` extension over a verified SSL connection: ssl_is_used,
ssl_version, ssl_cipher, ssl_client_cert_present, ssl_client_serial,
ssl_client_dn_field, ssl_issuer_dn/field, ssl_extension_info, and the
``sslcertmode`` connection option. Results are cross-checked against
``pg_stat_ssl`` where applicable.

Requires an OpenSSL build and ``ssl`` in PG_TEST_EXTRA.
"""

import pytest

from libpq import LibpqError
from pypg import check_pg_config, require_test_extras

pytestmark = require_test_extras("ssl")

SERVERHOSTADDR = "127.0.0.1"
SERVERHOSTCIDR = "127.0.0.1/32"


def test_sslinfo(create_pg, ssl_server):
    if not check_pg_config("#define USE_OPENSSL 1"):
        pytest.skip("OpenSSL not supported by this build")

    supports_sslcertmode_require = check_pg_config(
        "#define HAVE_SSL_CTX_SET_CERT_CB 1"
    )

    node = create_pg("primary", hostaddr=SERVERHOSTADDR)
    ssl_server.configure_test_server_for_ssl(
        node, SERVERHOSTADDR, SERVERHOSTCIDR, "trust", extensions=["sslinfo"]
    )

    # We aren't using any CRLs in this suite so we can keep using server-revoked
    # as the server certificate for a simple client.crt connection, much like
    # how the 001 test does.
    ssl_server.switch_server_cert(node, certfile="server-revoked")

    # Default SSL parameters, protecting the tests against any defaults the
    # environment may have in ~/.postgresql/.
    def common(**extra):
        opts = dict(
            sslkey="invalid",
            sslcert=ssl_server.cert("client_ext.crt"),
            sslrootcert=ssl_server.cert("root+server_ca.crt"),
            sslcrl="invalid",
            sslcrldir="invalid",
            sslmode="require",
            dbname="certdb",
            hostaddr=SERVERHOSTADDR,
            host="localhost",
            user="ssltestuser",
        )
        opts["sslkey"] = ssl_server.sslkey("client_ext.key")
        opts.update(extra)
        return opts

    # No-client-cert connection used by a couple of checks.
    def no_cert(**extra):
        opts = dict(
            sslkey="invalid",
            sslcert="invalid",
            sslrootcert=ssl_server.cert("root+server_ca.crt"),
            sslcrl="invalid",
            sslcrldir="invalid",
            sslmode="require",
            dbname="trustdb",
            hostaddr=SERVERHOSTADDR,
            host="localhost",
            user="ssltestuser",
        )
        opts.update(extra)
        return opts

    def query(sql, opts):
        with node.connect(**opts) as c:
            return c.sql(sql)

    # Make sure we can connect even though previous test suites have established
    # this.
    with node.connect(**common()):
        pass

    assert query("SELECT ssl_is_used();", common()) is True, (
        "ssl_is_used() for TLS connection"
    )

    assert (
        query(
            "SELECT ssl_version();",
            common(
                ssl_min_protocol_version="TLSv1.2",
                ssl_max_protocol_version="TLSv1.2",
            ),
        )
        == "TLSv1.2"
    ), "ssl_version() correctly returning TLS protocol"

    assert (
        query(
            "SELECT ssl_cipher() = cipher FROM pg_stat_ssl "
            "WHERE pid = pg_backend_pid();",
            common(),
        )
        is True
    ), "ssl_cipher() compared with pg_stat_ssl"

    assert query("SELECT ssl_client_cert_present();", common()) is True, (
        "ssl_client_cert_present() for connection with cert"
    )

    assert query("SELECT ssl_client_cert_present();", no_cert()) is False, (
        "ssl_client_cert_present() for connection without cert"
    )

    assert (
        query(
            "SELECT ssl_client_serial() = client_serial FROM pg_stat_ssl "
            "WHERE pid = pg_backend_pid();",
            common(),
        )
        is True
    ), "ssl_client_serial() compared with pg_stat_ssl"

    # An invalid field raises an error.
    with node.connect(**common()) as c:
        with pytest.raises(LibpqError):
            c.sql("SELECT ssl_client_dn_field('invalid');")

    assert query("SELECT ssl_client_dn_field('commonName');", no_cert()) is None, (
        "ssl_client_dn_field() for connection without cert"
    )

    assert (
        query(
            "SELECT '/CN=' || ssl_client_dn_field('commonName') = client_dn "
            "FROM pg_stat_ssl WHERE pid = pg_backend_pid();",
            common(),
        )
        is True
    ), "ssl_client_dn_field() for commonName"

    assert (
        query(
            "SELECT ssl_issuer_dn() = issuer_dn FROM pg_stat_ssl "
            "WHERE pid = pg_backend_pid();",
            common(),
        )
        is True
    ), "ssl_issuer_dn() for connection with cert"

    assert (
        query(
            "SELECT '/CN=' || ssl_issuer_field('commonName') = issuer_dn "
            "FROM pg_stat_ssl WHERE pid = pg_backend_pid();",
            common(),
        )
        is True
    ), "ssl_issuer_field() for commonName"

    assert query(
        "SELECT value, critical FROM ssl_extension_info() "
        "WHERE name = 'basicConstraints';",
        common(),
    ) == ("CA:FALSE", True), "extract extension from cert"

    # Sanity tests for sslcertmode, using ssl_client_cert_present().
    cases = [
        ("sslcertmode=allow", {"sslcertmode": "allow"}, True),
        (
            "sslcertmode=allow sslcert=invalid",
            {"sslcertmode": "allow", "sslcert": "invalid"},
            False,
        ),
        ("sslcertmode=disable", {"sslcertmode": "disable"}, False),
    ]
    if supports_sslcertmode_require:
        cases.append(("sslcertmode=require", {"sslcertmode": "require"}, True))

    for label, opts, present in cases:
        assert (
            query("SELECT ssl_client_cert_present();", common(dbname="trustdb", **opts))
            is present
        ), f"ssl_client_cert_present() for {label}"
