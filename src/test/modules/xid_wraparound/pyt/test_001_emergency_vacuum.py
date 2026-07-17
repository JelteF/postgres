# Copyright (c) 2023-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/xid_wraparound/t/001_emergency_vacuum.pl.

Tests wraparound emergency (failsafe) autovacuum: an old transaction holds
relfrozenxid back, XIDs are consumed to the brink of wraparound, and after the
old transaction commits, autovacuum freezes the tables via the failsafe path.
"""

import re

from pypg import require_test_extras

pytestmark = require_test_extras("xid_wraparound")

TABLES = ["large", "large_trunc", "small", "small_trunc"]


def test_emergency_vacuum(create_pg):
    node = create_pg(
        "main",
        conf={
            "autovacuum_naptime": "1s",
            # so it's easier to verify the order of operations
            "autovacuum_max_workers": 1,
            "log_autovacuum_min_duration": 0,
        },
    )
    node.sql("CREATE EXTENSION xid_wraparound")

    # Tables for a few scenarios. Autovacuum is disabled on each so it runs
    # only to prevent wraparound.
    node.sql_batch(
        "CREATE TABLE large(id serial primary key, data text, "
        "filler text default repeat(random()::text, 10)) "
        "WITH (autovacuum_enabled = off)",
        "INSERT INTO large(data) SELECT generate_series(1,30000)",
        "CREATE TABLE large_trunc(id serial primary key, data text, "
        "filler text default repeat(random()::text, 10)) "
        "WITH (autovacuum_enabled = off)",
        "INSERT INTO large_trunc(data) SELECT generate_series(1,30000)",
        "CREATE TABLE small(id serial primary key, data text, "
        "filler text default repeat(random()::text, 10)) "
        "WITH (autovacuum_enabled = off)",
        "INSERT INTO small(data) SELECT generate_series(1,15000)",
        "CREATE TABLE small_trunc(id serial primary key, data text, "
        "filler text default repeat(random()::text, 10)) "
        "WITH (autovacuum_enabled = off)",
        "INSERT INTO small_trunc(data) SELECT generate_series(1,15000)",
    )

    # A background session holds a transaction open, preventing autovacuum from
    # advancing relfrozenxid and datfrozenxid.
    bg = node.connect()
    bg.sql_batch(
        "BEGIN",
        "DELETE FROM large WHERE id % 2 = 0",
        "DELETE FROM large_trunc WHERE id > 10000",
        "DELETE FROM small WHERE id % 2 = 0",
        "DELETE FROM small_trunc WHERE id > 1000",
    )

    # Consume 2 billion XIDs, to get very close to wraparound.
    node.sql("SELECT consume_xids_until('2000000000'::xid8)")
    # Make sure the latest completed XID is advanced.
    node.sql("INSERT INTO small(data) SELECT 1")

    # All databases should be old enough to trigger failsafe.
    assert node.sql(
        "SELECT datname, "
        "age(datfrozenxid) > current_setting('vacuum_failsafe_age')::int as old "
        "FROM pg_database ORDER BY 1"
    ) == [("postgres", True), ("template0", True), ("template1", True)]

    offset = node.current_log_position()

    # Finish the old transaction so vacuum freezing can advance relfrozenxid
    # and datfrozenxid again.
    bg.sql("COMMIT")
    bg.close()

    # Wait until autovacuum has processed all tables and advanced the
    # system-wide oldest XID.
    node.poll_query_until(
        "SELECT NOT EXISTS (SELECT * FROM pg_database WHERE "
        "age(datfrozenxid) > current_setting('autovacuum_freeze_max_age')::int)"
    )

    # The tables should be vacuumed.
    assert node.sql(
        "SELECT relname, "
        "age(relfrozenxid) > current_setting('autovacuum_freeze_max_age')::int "
        "FROM pg_class WHERE relname IN ('large', 'large_trunc', 'small', "
        "'small_trunc') ORDER BY 1"
    ) == [
        ("large", False),
        ("large_trunc", False),
        ("small", False),
        ("small_trunc", False),
    ]

    # The failsafe should have been triggered for each table.
    log = node.log_since(offset)
    for table in TABLES:
        assert re.search(
            rf'bypassing nonessential maintenance of table "postgres.public.{table}" '
            r"as a failsafe after \d+ index scans",
            log,
        ), f"failsafe vacuum triggered for {table}"
