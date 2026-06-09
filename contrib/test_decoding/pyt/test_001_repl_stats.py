# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of contrib/test_decoding/t/001_repl_stats.pl.

Tests that replication-slot statistics in pg_stat_replication_slots stay sane
across dropping slots, removing a slot's on-disk directory, lowering
max_replication_slots, and server restarts, and that a slot's stats entry is
dropped before the stats file is written at shutdown even while another session
holds a cached reference to it.
"""

import shutil

from pypg.bins import pg_controldata


def _slot_stats(node):
    return node.sql(
        "SELECT slot_name, total_txns > 0 AS total_txn, total_bytes > 0 AS total_bytes "
        "FROM pg_stat_replication_slots ORDER BY slot_name"
    )


def test_repl_stats(create_pg):
    node = create_pg("test", conf={"wal_level": "logical", "synchronous_commit": True})

    node.sql("CREATE TABLE test_repl_stat(col1 int)")

    for i in range(1, 5):
        node.sql(
            f"SELECT pg_create_logical_replication_slot('regression_slot{i}', "
            "'test_decoding')"
        )

    node.sql("INSERT INTO test_repl_stat values(generate_series(1, 5))")

    # Decode the changes on each slot so their stats accumulate.
    for i in range(1, 5):
        node.sql(
            f"SELECT data FROM pg_logical_slot_get_changes('regression_slot{i}', "
            "NULL, NULL, 'include-xids', '0', 'skip-empty-xacts', '1')"
        )

    # Wait for the statistics to be updated.
    node.poll_query_until(
        "SELECT count(slot_name) >= 4 FROM pg_stat_replication_slots "
        "WHERE slot_name ~ 'regression_slot' AND total_txns > 0 AND total_bytes > 0"
    )

    # Drop a slot and confirm the stats survive a restart.
    node.sql("SELECT pg_drop_replication_slot('regression_slot4')")
    node.stop()
    node.start()

    assert _slot_stats(node) == [
        ("regression_slot1", True, True),
        ("regression_slot2", True, True),
        ("regression_slot3", True, True),
    ], "check replication statistics are updated"

    # Remove a slot's on-disk directory and lower max_replication_slots so the
    # number of slots in the stats file exceeds shared memory; stats must still
    # be sane after restart.
    node.stop()
    shutil.rmtree(node.datadir / "pg_replslot" / "regression_slot3")
    node.append_conf(max_replication_slots=2)
    node.start()

    assert _slot_stats(node) == [
        ("regression_slot1", True, True),
        ("regression_slot2", True, True),
    ], "check replication statistics after removing the slot file"

    node.sql("DROP TABLE test_repl_stat")
    node.sql("SELECT pg_drop_replication_slot('regression_slot1')")
    node.sql("SELECT pg_drop_replication_slot('regression_slot2')")
    node.stop()

    # Slot-stats persistence in a single session: the slot is dropped and
    # recreated while a persistent session repeatedly peeks at its data, so it
    # holds a cached reference to the stats entry.
    node.start()

    slot = "regression_slot5"
    node.sql(f"SELECT pg_create_logical_replication_slot('{slot}', 'test_decoding')")

    bg = node.connect()
    bg.sql(f"SELECT pg_logical_slot_peek_binary_changes('{slot}', NULL, NULL)")

    # Drop the slot; its stats entry survives because bg still references it.
    node.sql(f"SELECT pg_drop_replication_slot('{slot}')")
    # Recreate it; the stats entry is reinitialized, no longer marked dropped.
    node.sql(f"SELECT pg_create_logical_replication_slot('{slot}', 'test_decoding')")
    # Peek again; bg's cached reference is refreshed to the reinitialized entry.
    bg.sql(f"SELECT pg_logical_slot_peek_binary_changes('{slot}', NULL, NULL)")
    node.sql(f"SELECT pg_drop_replication_slot('{slot}')")

    # Shut down with bg still connected, so the server drops the slot's stats
    # entry before writing the stats file.
    node.stop()

    out = pg_controldata.capture(node.datadir)
    assert "Database cluster state:" in out and "shut down" in out, "node shut down ok"
    assert (node.datadir / "pg_stat" / "pgstat.stat").is_file(), (
        "stats file must exist after shutdown"
    )

    bg.close()
