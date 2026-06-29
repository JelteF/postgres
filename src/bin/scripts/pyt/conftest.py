# Copyright (c) 2026, PostgreSQL Global Development Group

"""Shared fixtures for the src/bin/scripts client-program tests.

These tests assert on the SQL each program issues by grepping the server log
(the pytest port of Perl's ``issues_sql_like``), which needs ``log_statement =
all``. ``PostgreSQL::Test::Cluster`` turns that on for every node by default;
the pytest framework deliberately does not (it would add log noise to every
other suite), so it is enabled here for just this suite.
"""

import pytest


@pytest.fixture(scope="module", autouse=True)
def _enable_statement_logging(pg_server_module):
    pg_server_module.append_conf(log_statement="all")
    pg_server_module.pg_ctl("reload")
