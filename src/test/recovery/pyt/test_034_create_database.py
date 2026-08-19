# Copyright (c) 2023-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/034_create_database.pl.

Tests WAL replay for CREATE DATABASE ... STRATEGY WAL_LOG: DDL run on the
template database after the new database is created (which modifies pg_class)
must persist a crash, since WAL_LOG copies the template's pg_class directly.
"""


def test_create_database(create_pg):
    node = create_pg("node")

    db_template = "template1"
    db_new = "test_db_1"

    node.sql(f"CREATE DATABASE {db_new} STRATEGY WAL_LOG TEMPLATE {db_template}")

    # This table should persist on the template database.
    node.sql_oneshot("CREATE TABLE tab_db_after_create_1 (a INT)", dbname=db_template)

    # Flush the changes, then crash and replay them.
    node.sql("CHECKPOINT")
    node.stop("immediate")
    node.start()

    assert (
        node.sql_oneshot(
            "SELECT count(*) FROM pg_class WHERE relname LIKE 'tab_db_%'",
            dbname=db_template,
        )
        == 1
    ), "table exists on template after crash, with checkpoint"

    # The new database should have no tables from the template.
    assert (
        node.sql_oneshot(
            "SELECT count(*) FROM pg_class WHERE relname LIKE 'tab_db_%'",
            dbname=db_new,
        )
        == 0
    ), "no tables from template on new database after crash"
