# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""PostgresServer behaviour that isn't about running a single query.

Per-test isolation of module-scoped servers, and the default_db attribute.
"""

import pytest


@pytest.fixture(scope="module")
def leaky_setup_node(create_pg_module):
    """A module-scoped server whose setup runs on the node's cached connection.

    This is the tempting way to write such a fixture, and the one that used to
    leak: sql() opens its cached connection in whatever cleanup context is
    current, so opening it here tied it to the module and handed the same
    session to every test in it.
    """
    node = create_pg_module("leaky_setup")
    node.sql("CREATE TABLE setup_marker(a int)")
    node.sql("SET application_name = 'set_during_module_setup'")
    return node


def test_setup_session_state_does_not_reach_tests(leaky_setup_node):
    """A test does not inherit the session the module fixture used."""
    assert leaky_setup_node.sql("SHOW application_name") != "set_during_module_setup"

    # The setup's committed work is of course still there.
    assert leaky_setup_node.sql("SELECT count(*) FROM setup_marker") == 0


def test_a_leaves_session_state(leaky_setup_node):
    leaky_setup_node.sql("SET application_name = 'set_by_test_a'")
    leaky_setup_node.sql("CREATE TEMPORARY TABLE only_in_test_a(a int)")

    assert leaky_setup_node.sql("SHOW application_name") == "set_by_test_a"


def test_b_does_not_see_test_a_state(leaky_setup_node):
    """Runs after test_a; must not see its GUC or its temporary table.

    Depends on pytest running these in file order, which it does for tests in
    one module.
    """
    assert leaky_setup_node.sql("SHOW application_name") != "set_by_test_a"

    assert leaky_setup_node.sql("SELECT to_regclass('only_in_test_a')") is None, (
        "temporary table from the previous test is still visible"
    )


def test_default_db_switches_queries(pg):
    """default_db redirects sql() and connect() without repeating dbname=."""
    pg.sql("CREATE DATABASE otherdb")
    assert pg.sql("SELECT current_database()") == "postgres"

    pg.default_db = "otherdb"

    assert pg.sql("SELECT current_database()") == "otherdb"
    assert pg.sql_oneshot("SELECT current_database()") == "otherdb"
    assert pg.poll_query_until("SELECT current_database()", expected="otherdb")
    with pg.connect() as conn:
        assert conn.sql("SELECT current_database()") == "otherdb"

    # An explicit dbname still wins, so a test can reach back.
    assert pg.sql_oneshot("SELECT current_database()", dbname="postgres") == "postgres"


def test_default_db_drops_the_cached_connection(pg):
    """Assigning default_db must not leave sql() on the old database.

    The connection sql() caches is attached to whichever database it was opened
    against, so the setter has to close it; otherwise queries would keep going
    to the old one until something else happened to invalidate it.
    """
    pg.sql("CREATE DATABASE yetanotherdb")
    pg.sql("SET application_name = 'before_switch'")

    pg.default_db = "yetanotherdb"

    assert pg.sql("SELECT current_database()") == "yetanotherdb"
    assert pg.sql("SHOW application_name") != "before_switch"
