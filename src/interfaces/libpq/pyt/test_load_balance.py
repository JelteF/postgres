# Copyright (c) 2025, PostgreSQL Global Development Group

"""
Tests for load_balance_hosts connection parameter.

These tests verify that libpq correctly handles load balancing across multiple
PostgreSQL servers specified in the connection string.
"""

import platform
import re

import pytest

from libpq import LibpqError
import pypg


@pytest.fixture(scope="module")
def load_balance_nodes_hostlist(create_pg_module):
    """
    Create 3 PostgreSQL nodes with different socket directories.

    Each node has its own Unix socket directory for isolation.
    Returns a tuple of (nodes, connect).
    """
    nodes = [create_pg_module() for _ in range(3)]

    hostlist = ",".join(node.host for node in nodes)
    portlist = ",".join(str(node.port) for node in nodes)

    def connect(**kwargs):
        return nodes[0].connect(host=hostlist, port=portlist, **kwargs)

    return nodes, connect


@pytest.fixture(scope="module")
def load_balance_nodes_dns(create_pg_module):
    """
    Create 3 PostgreSQL nodes on the same port but different IP addresses.

    Uses 127.0.0.1, 127.0.0.2, 127.0.0.3 with a shared port, so that
    connections to 'pg-loadbalancetest' can be load balanced via DNS.

    Since setting up a DNS server is more effort than we consider reasonable to
    run this test, this situation is instead imitated by using a hosts file
    where a single hostname maps to multiple different IP addresses. This test
    requires the administrator to add the following lines to the hosts file (if
    we detect that this hasn't happened we skip the test):

    127.0.0.1 pg-loadbalancetest
    127.0.0.2 pg-loadbalancetest
    127.0.0.3 pg-loadbalancetest

    Windows or Linux are required to run this test because these OSes allow
    binding to 127.0.0.2 and 127.0.0.3 addresses by default, but other OSes
    don't. We need to bind to different IP addresses, so that we can use these
    different IP addresses in the hosts file.

    The hosts file needs to be prepared before running this test. We don't do
    it on the fly, because it requires root permissions to change the hosts
    file. In CI we set up the previously mentioned rules in the hosts file, so
    that this load balancing method is tested.

    Requires PG_TEST_EXTRA=load_balance because it requires this manual hosts
    file configuration and also uses TCP with trust auth, which is potentially
    unsafe on multiuser systems.
    """
    pypg.skip_unless_test_extras("load_balance")

    if platform.system() not in ("Linux", "Windows"):
        pytest.skip("DNS load balance test only supported on Linux and Windows")

    if platform.system() == "Windows":
        hosts_path = r"c:\Windows\System32\Drivers\etc\hosts"
    else:
        hosts_path = "/etc/hosts"

    try:
        with open(hosts_path) as f:
            hosts_content = f.read()
    except (OSError, IOError):
        pytest.skip(f"Could not read hosts file: {hosts_path}")

    count = len(re.findall(r"127\.0\.0\.[1-3]\s+pg-loadbalancetest", hosts_content))
    if count != 3:
        pytest.skip("hosts file not prepared for DNS load balance test")

    first_node = create_pg_module(hostaddr="127.0.0.1")
    nodes = [
        first_node,
        create_pg_module(hostaddr="127.0.0.2", port=first_node.port),
        create_pg_module(hostaddr="127.0.0.3", port=first_node.port),
    ]

    # Allow trust authentication for TCP connections from loopback
    for node in nodes:
        hba_path = node.datadir / "pg_hba.conf"
        with open(hba_path, "r") as f:
            original_content = f.read()
        with open(hba_path, "w") as f:
            f.write("host all all 127.0.0.0/8 trust\n")
            f.write(original_content)
        node.pg_ctl("reload")

    def connect(**kwargs):
        return nodes[0].connect(host="pg-loadbalancetest", **kwargs)

    return nodes, connect


@pytest.fixture(scope="module", params=["hostlist", "dns"])
def load_balance_nodes(request):
    """
    Parametrized fixture providing both load balancing test environments.
    """
    return request.getfixturevalue(f"load_balance_nodes_{request.param}")


def test_load_balance_hosts_invalid_value(load_balance_nodes):
    """load_balance_hosts doesn't accept unknown values."""
    _, connect = load_balance_nodes

    with pytest.raises(
        LibpqError, match='invalid load_balance_hosts value: "doesnotexist"'
    ):
        connect(load_balance_hosts="doesnotexist")


def test_load_balance_hosts_disable(load_balance_nodes):
    """load_balance_hosts=disable always connects to the first node."""
    nodes, connect = load_balance_nodes

    with nodes[0].log_contains("connection received"):
        connect(load_balance_hosts="disable")


def test_load_balance_hosts_random_distribution(load_balance_nodes):
    """load_balance_hosts=random distributes connections across all nodes."""
    nodes, connect = load_balance_nodes

    for _ in range(50):
        connect(load_balance_hosts="random")

    occurrences = [
        len(re.findall("connection received", node.log_content())) for node in nodes
    ]

    # Statistically, each node should receive at least one connection.
    # The probability of any node receiving 0 connections is (2/3)^50 ≈ 1.57e-9
    assert occurrences[0] > 0, "node1 should receive at least one connection"
    assert occurrences[1] > 0, "node2 should receive at least one connection"
    assert occurrences[2] > 0, "node3 should receive at least one connection"
    assert sum(occurrences) == 50, "total connections should be 50"


def test_load_balance_hosts_failover(load_balance_nodes):
    """load_balance_hosts continues trying hosts until it finds a working one."""
    nodes, connect = load_balance_nodes

    nodes[0].stop()
    nodes[1].stop()

    with nodes[2].log_contains("connection received"):
        connect(load_balance_hosts="disable")

    with nodes[2].log_contains("connection received", times=5):
        for _ in range(5):
            connect(load_balance_hosts="random")
