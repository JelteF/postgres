# Copyright (c) 2025, PostgreSQL Global Development Group

"""
Tests demonstrating multi-server functionality using create_pg fixture.

These tests verify that the pytest infrastructure correctly handles
multiple PostgreSQL server instances within a single test, and that
module-scoped servers persist across tests.
"""

import pytest


def test_multiple_servers_basic(create_pg):
    """Test that we can create and connect to multiple servers."""
    node1 = create_pg("primary")
    node2 = create_pg("secondary")

    conn1 = node1.connect()
    conn2 = node2.connect()

    # Each server should have its own data directory
    datadir1 = conn1.sql("SHOW data_directory")
    datadir2 = conn2.sql("SHOW data_directory")
    assert datadir1 != datadir2

    # Each server should be listening on a different port
    assert node1.port != node2.port


@pytest.fixture(scope="module")
def shared_server(create_pg_module):
    """A server shared across all tests in this module."""
    server = create_pg_module("shared")
    server.sql("CREATE TABLE module_state (value int DEFAULT 0)")
    return server


def test_module_server_create_row(shared_server):
    """First test: create a row in the shared server."""
    shared_server.connect().sql("INSERT INTO module_state VALUES (42)")


def test_module_server_see_row(shared_server):
    """Second test: verify we see the row from the previous test."""
    assert shared_server.connect().sql("SELECT value FROM module_state") == 42
