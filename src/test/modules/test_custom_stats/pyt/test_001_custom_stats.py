# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/test_custom_stats/t/001_custom_stats.pl.

Tests custom pgstats (both variable- and fixed-sized): creation, updates and
reporting, persistence across a clean restart, loss after crash recovery, and
resets for fixed-sized stats.
"""


def test_custom_stats(create_pg):
    node = create_pg(
        "custom_stats",
        conf={
            "shared_preload_libraries": "test_custom_var_stats, test_custom_fixed_stats"
        },
    )

    node.sql("CREATE EXTENSION test_custom_var_stats")
    node.sql("CREATE EXTENSION test_custom_fixed_stats")

    # Verify custom stats kinds appear in pg_stat_kind_info.
    assert node.sql(
        "SELECT id, name, builtin, fixed_amount, accessed_across_databases, "
        "write_to_file FROM pg_stat_kind_info WHERE name LIKE 'test_custom%' "
        "ORDER BY id"
    ) == [
        (25, "test_custom_var_stats", False, False, True, True),
        (26, "test_custom_fixed_stats", False, True, False, True),
    ], "custom stats kinds visible in pg_stat_kind_info"

    # Stats are only flushed to shared memory when a backend exits, so each
    # update needs its own connection for the counters to become visible.
    for entry in ("entry1", "entry2", "entry3", "entry4"):
        node.sql_oneshot(
            f"SELECT test_custom_stats_var_create('{entry}', 'Test {entry}')"
        )

    # Update counters: entry1=2, entry2=3, entry3=2, entry4=3, fixed=3.
    updates = ["entry1"] * 2 + ["entry2"] * 3 + ["entry3"] * 2 + ["entry4"] * 3
    for entry in updates:
        node.sql_oneshot(f"SELECT test_custom_stats_var_update('{entry}')")
    for _ in range(3):
        node.sql_oneshot("SELECT test_custom_stats_fixed_update()")

    def var_report(entry):
        return node.sql(f"SELECT * FROM test_custom_stats_var_report('{entry}')")

    assert var_report("entry1") == ("entry1", 2, "Test entry1")
    assert var_report("entry2") == ("entry2", 3, "Test entry2")
    assert var_report("entry3") == ("entry3", 2, "Test entry3")
    assert var_report("entry4") == ("entry4", 3, "Test entry4")
    assert node.sql("SELECT * FROM test_custom_stats_fixed_report()") == (3, None)

    # Dropping variable-sized stats removes the entry.
    node.sql_oneshot("SELECT * FROM test_custom_stats_var_drop('entry3')")
    assert var_report("entry3") == []
    node.sql_oneshot("SELECT * FROM test_custom_stats_var_drop('entry4')")
    assert var_report("entry4") == []

    # Persistence across a clean restart.
    node.stop()
    node.start()
    assert var_report("entry1") == ("entry1", 2, "Test entry1")
    assert var_report("entry2") == ("entry2", 3, "Test entry2")
    assert node.sql("SELECT * FROM test_custom_stats_fixed_report()") == (3, None)

    # Loss after crash recovery.
    node.stop("immediate")
    node.start()
    assert var_report("entry1") == []
    assert var_report("entry2") == []
    # Crash recovery sets the reset timestamp on fixed-sized stats.
    assert (
        node.sql(
            "SELECT numcalls FROM test_custom_stats_fixed_report() "
            "WHERE stats_reset IS NOT NULL"
        )
        == 0
    )

    # Manual reset of fixed-sized stats.
    for _ in range(3):
        node.sql_oneshot("SELECT test_custom_stats_fixed_update()")
    assert node.sql("SELECT numcalls FROM test_custom_stats_fixed_report()") == 3
    node.sql_oneshot("SELECT test_custom_stats_fixed_reset()")
    assert (
        node.sql(
            "SELECT numcalls FROM test_custom_stats_fixed_report() "
            "WHERE stats_reset IS NOT NULL"
        )
        == 0
    )
