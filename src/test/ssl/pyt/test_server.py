# Copyright (c) 2025, PostgreSQL Global Development Group

import re
import socket
import ssl
import struct

import pytest

import pypg

# This suite opens up local TCP ports and is hidden behind PG_TEST_EXTRA=ssl.
pytestmark = pypg.require_test_extra("ssl")


#
# Test Fixtures
#


#
# Tests
#


# For use with the `creds` parameter below.
CLIENT = "client"
SERVER = "server"


# fmt: off
@pytest.mark.parametrize(
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
)
# fmt: on
def test_direct_ssl_certificate_authentication(
    pg,
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

    with pg.reloading() as s:
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
    addr = (pg.hostaddr, pg.port)
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
