# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/006_logical_decoding.pl.

Logical decoding via the SQL interface and pg_recvlogical. Most logical
decoding tests live in contrib/test_decoding; this module covers cases that
need server restarts, walsender error paths, cross-database slot access,
dropping databases with logical slots, slot advancing durability, and
pg_stat_replication_slots resets.
"""

import subprocess

from libpq import LibpqError
import pytest

EXPECTED = [
    "BEGIN",
    "table public.decoding_test: INSERT: x[integer]:1 y[text]:'1'",
    "table public.decoding_test: INSERT: x[integer]:2 y[text]:'2'",
    "table public.decoding_test: INSERT: x[integer]:3 y[text]:'3'",
    "table public.decoding_test: INSERT: x[integer]:4 y[text]:'4'",
    "COMMIT",
]


def test_logical_decoding(create_pg, pg_bin):
    primary = create_pg("primary", allows_streaming=True, conf=["wal_level = logical"])

    primary.sql("CREATE TABLE decoding_test(x integer, y text)")
    primary.sql(
        "SELECT pg_create_logical_replication_slot('test_slot', 'test_decoding')"
    )

    # Cover the walsender error shutdown code: a logical slot can only be read
    # from the database in which it was created.
    with primary.connect(dbname="template1", replication="database") as repl:
        with pytest.raises(
            LibpqError,
            match='replication slot "test_slot" was not created in this database',
        ):
            repl.sql("START_REPLICATION SLOT test_slot LOGICAL 0/0")

    with primary.connect(dbname="template1", replication="database") as repl:
        with pytest.raises(
            LibpqError,
            match="cannot use READ_REPLICATION_SLOT with a logical replication slot",
        ):
            repl.sql("READ_REPLICATION_SLOT test_slot")

    # A walsender not using a database connection must not allow logical
    # decoding.
    with primary.connect(dbname="template1", replication="true") as repl:
        with pytest.raises(
            LibpqError, match="logical decoding requires a database connection"
        ):
            repl.sql("START_REPLICATION SLOT s1 LOGICAL 0/1")

    primary.sql(
        "INSERT INTO decoding_test(x,y) SELECT s, s::text FROM generate_series(1,10) s"
    )

    # Basic decoding works: 10 inserts plus BEGIN/COMMIT.
    rows = primary.sql("SELECT pg_logical_slot_get_changes('test_slot', NULL, NULL)")
    assert len(rows) == 12, "Decoding produced 12 rows inc BEGIN/COMMIT"

    # A clean shutdown should never repeat changes already consumed via the SQL
    # decoding interface. After the restart there are no new writes.
    primary.pg_ctl("restart")
    assert (
        primary.sql("SELECT pg_logical_slot_get_changes('test_slot', NULL, NULL)") == []
    ), "Decoding after fast restart repeats no rows"

    # Verify the SQL interface and pg_recvlogical produce identical results.
    primary.sql(
        "INSERT INTO decoding_test(x,y) SELECT s, s::text FROM generate_series(1,4) s"
    )
    assert (
        primary.sql(
            "SELECT data FROM pg_logical_slot_peek_changes('test_slot', NULL, NULL, "
            "'include-xids', '0', 'skip-empty-xacts', '1')"
        )
        == EXPECTED
    ), "got expected output from SQL decoding session"

    endpos = primary.sql(
        "SELECT lsn FROM pg_logical_slot_peek_changes('test_slot', NULL, NULL) "
        "ORDER BY lsn DESC LIMIT 1"
    )

    # Insert rows after endpos, which we won't read.
    primary.sql(
        "INSERT INTO decoding_test(x,y) SELECT s, s::text FROM generate_series(5,50) s"
    )

    plugin_opts = {"include-xids": "0", "skip-empty-xacts": "1"}
    assert primary.pg_recvlogical_upto("test_slot", endpos, options=plugin_opts) == (
        "\n".join(EXPECTED)
    ), "got same expected output from pg_recvlogical decoding session"

    primary.poll_query_until(
        "SELECT EXISTS (SELECT 1 FROM pg_replication_slots "
        "WHERE slot_name = 'test_slot' AND active_pid IS NULL)"
    )

    # The previous run confirmed the changes, so a second run reads nothing.
    assert primary.pg_recvlogical_upto("test_slot", endpos, options=plugin_opts) == "", (
        "pg_recvlogical acknowledged changes"
    )

    primary.sql("CREATE DATABASE otherdb")

    # Replaying a logical slot from another database fails.
    with pytest.raises(
        LibpqError, match='replication slot "test_slot" was not created in this database'
    ):
        primary.sql(
            "SELECT lsn FROM pg_logical_slot_peek_changes('test_slot', NULL, NULL) "
            "ORDER BY lsn DESC LIMIT 1",
            dbname="otherdb",
        )

    primary.sql(
        "SELECT pg_create_logical_replication_slot('otherdb_slot', 'test_decoding')",
        dbname="otherdb",
    )

    # A database with an active logical slot can't be dropped. Hold the slot
    # active with a streaming pg_recvlogical for the duration.
    recv = subprocess.Popen(
        [
            str(pg_bin.bindir / "pg_recvlogical"),
            "--dbname", primary.connstr("otherdb"),
            "--slot", "otherdb_slot",
            "--file", "-",
            "--start",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        primary.poll_query_until(
            "SELECT EXISTS (SELECT 1 FROM pg_replication_slots "
            "WHERE slot_name = 'otherdb_slot' AND active_pid IS NOT NULL)",
            dbname="otherdb",
        )
        with pytest.raises(
            LibpqError, match="is used by an active logical replication slot"
        ):
            primary.sql("DROP DATABASE otherdb")
    finally:
        recv.kill()
        recv.wait()

    assert (
        primary.sql(
            "SELECT plugin FROM pg_replication_slots WHERE slot_name = 'otherdb_slot'"
        )
        == "test_decoding"
    ), "logical slot still exists"

    primary.poll_query_until(
        "SELECT EXISTS (SELECT 1 FROM pg_replication_slots "
        "WHERE slot_name = 'otherdb_slot' AND active_pid IS NULL)",
        dbname="otherdb",
    )

    # With the slot inactive, dropping the database succeeds and removes the slot.
    primary.sql("DROP DATABASE otherdb")
    assert (
        primary.sql(
            "SELECT plugin FROM pg_replication_slots WHERE slot_name = 'otherdb_slot'"
        )
        == []
    ), "logical slot was actually dropped with DB"

    # Logical slot advancing and its durability. Passing failover=true should
    # have no impact on advancing.
    primary.sql(
        "SELECT pg_create_logical_replication_slot('logical_slot', 'test_decoding', "
        "false, false, true)"
    )
    primary.sql(
        "CREATE TABLE tab_logical_slot (a int);"
        "INSERT INTO tab_logical_slot VALUES (generate_series(1,10))"
    )
    current_lsn = primary.sql("SELECT pg_current_wal_lsn()")
    primary.sql(
        f"SELECT pg_replication_slot_advance('logical_slot', '{current_lsn}'::pg_lsn)"
    )
    restart_lsn_pre = primary.sql(
        "SELECT restart_lsn FROM pg_replication_slots WHERE slot_name = 'logical_slot'"
    )

    # Slot advance should persist across clean restarts.
    primary.pg_ctl("restart")
    restart_lsn_post = primary.sql(
        "SELECT restart_lsn FROM pg_replication_slots WHERE slot_name = 'logical_slot'"
    )
    assert restart_lsn_pre == restart_lsn_post, (
        "logical slot advance persists across restarts"
    )

    # Test that reset works for pg_stat_replication_slots. slot1 has had decoding
    # activity; slot2 has not been reset yet.
    assert primary.sql(
        "SELECT total_bytes > 0, stats_reset IS NULL FROM pg_stat_replication_slots "
        "WHERE slot_name = 'test_slot'"
    ) == (True, True), "total_bytes > 0 and stats_reset is NULL for test_slot"

    primary.sql("SELECT pg_stat_reset_replication_slot('test_slot')")
    reset1 = primary.sql(
        "SELECT stats_reset::text FROM pg_stat_replication_slots "
        "WHERE slot_name = 'test_slot'"
    )
    primary.sql("SELECT pg_stat_reset_replication_slot('test_slot')")
    assert primary.sql(
        f"SELECT stats_reset > '{reset1}'::timestamptz, total_bytes = 0 "
        "FROM pg_stat_replication_slots WHERE slot_name = 'test_slot'"
    ) == (True, True), (
        "reset timestamp is later after the second reset and total_bytes is 0"
    )

    assert primary.sql(
        "SELECT stats_reset IS NULL FROM pg_stat_replication_slots "
        "WHERE slot_name = 'logical_slot'"
    ) is True, "stats_reset is NULL for logical_slot before reset"

    reset1 = primary.sql(
        "SELECT stats_reset::text FROM pg_stat_replication_slots "
        "WHERE slot_name = 'test_slot'"
    )
    # Reset stats for all replication slots.
    primary.sql("SELECT pg_stat_reset_replication_slot(NULL)")
    assert primary.sql(
        "SELECT stats_reset IS NOT NULL FROM pg_stat_replication_slots "
        "WHERE slot_name = 'logical_slot'"
    ) is True, "stats_reset is not NULL for logical_slot after reset all"
    assert primary.sql(
        f"SELECT stats_reset > '{reset1}'::timestamptz FROM pg_stat_replication_slots "
        "WHERE slot_name = 'test_slot'"
    ) is True, "reset timestamp is later after resetting test_slot again"
