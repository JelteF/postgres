# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/036_truncated_dropped.pl.

Tests recovery where on-disk files are shorter than the WAL records being
replayed expect, e.g. replaying PRUNE records for a relation that was
subsequently truncated or dropped. Each scenario builds some state, performs an
immediate (crash) shutdown, and confirms recovery succeeds (and, where
relevant, the table contents are correct).

Statements are issued individually because VACUUM (and the autocommit semantics
the Perl test relies on) cannot run inside the implicit transaction that libpq
would wrap a multi-statement query in.
"""


def test_truncated_dropped(create_pg):
    node = create_pg("n1", conf={"wal_level": "replica", "autovacuum": False})

    def run_each(*statements):
        for stmt in statements:
            node.sql(stmt)

    def crash_and_restart():
        node.stop("immediate")
        node.start()

    # PRUNE records for a pre-existing (checkpointed), then dropped, relation.
    run_each(
        "CREATE TABLE truncme(i int) WITH (fillfactor = 50)",
        "INSERT INTO truncme SELECT generate_series(1, 1000)",
        "UPDATE truncme SET i = 1",
        "CHECKPOINT",  # ensure relation exists at start of recovery
        "VACUUM truncme",  # generate prune records
        "DROP TABLE truncme",
    )
    crash_and_restart()

    # PRUNE records for a newly created, then dropped, relation.
    run_each(
        "CREATE TABLE truncme(i int) WITH (fillfactor = 50)",
        "INSERT INTO truncme SELECT generate_series(1, 1000)",
        "UPDATE truncme SET i = 1",
        "VACUUM truncme",
        "DROP TABLE truncme",
    )
    crash_and_restart()

    # PRUNE records affecting a truncated block, with FPIs.
    run_each(
        "CREATE TABLE truncme(i int) WITH (fillfactor = 50)",
        "INSERT INTO truncme SELECT generate_series(1, 1000)",
        "UPDATE truncme SET i = 1",
        "CHECKPOINT",  # generate FPIs
        "VACUUM truncme",  # generate prune records
        "TRUNCATE truncme",  # make blocks non-existing
        "INSERT INTO truncme SELECT generate_series(1, 10)",
    )
    crash_and_restart()
    assert node.sql("select count(*), sum(i) FROM truncme") == (10, 55), (
        "table contents as expected after recovery"
    )
    node.sql("DROP TABLE truncme")

    # PRUNE records for blocks later truncated, without FPIs.
    run_each(
        "CREATE TABLE truncme(i int) WITH (fillfactor = 50)",
        "INSERT INTO truncme SELECT generate_series(1, 1000)",
        "UPDATE truncme SET i = 1",
        "VACUUM truncme",
        "TRUNCATE truncme",
        "INSERT INTO truncme SELECT generate_series(1, 10)",
    )
    crash_and_restart()
    assert node.sql("select count(*), sum(i) FROM truncme") == (10, 55), (
        "table contents as expected after recovery"
    )
    node.sql("DROP TABLE truncme")

    # Partial truncation via VACUUM.
    run_each(
        "CREATE TABLE truncme(i int) WITH (fillfactor = 50)",
        "INSERT INTO truncme SELECT generate_series(1, 1000)",
        "UPDATE truncme SET i = i + 1",
        "DELETE FROM truncme WHERE i > 500",  # mix of pre/post truncation rows
        "VACUUM truncme",  # should truncate relation
        "INSERT INTO truncme SELECT generate_series(1000, 1010)",
    )
    crash_and_restart()
    assert node.sql("select count(*), sum(i), min(i), max(i) FROM truncme") == (
        510,
        136304,
        2,
        1010,
    ), "table contents as expected after recovery"
    node.sql("DROP TABLE truncme")
