# Copyright (c) 2026, PostgreSQL Global Development Group

"""Port of src/test/modules/test_autovacuum/t/001_parallel_autovacuum.pl.

Tests parallel autovacuum: that a table with enough indexes and the right
reloptions is vacuumed in parallel, and that the leader propagates cost-based
delay parameters to the parallel workers.
"""

import pypg

POINT = "autovacuum-start-parallel-vacuum"


@pypg.require_injection_points()
def test_parallel_autovacuum(create_pg):
    # One autovacuum worker, autovacuum logging only on the test table, so the
    # log checks below match only the expected activity.
    #
    # Autovacuum must trigger for nothing but test_autovac: with
    # min_parallel_index_scan_size = 0, any table with more than one index is
    # eligible for parallel vacuum, including catalog tables in template1 etc.
    # (which do get autovacuumed in a fresh cluster, from initdb's insert
    # stats). If one of those grabs the injection point in test 2, the wakeup
    # is wasted on it and the actual test_autovac vacuum then blocks on the
    # still-attached 'wait' point until the test times out (seen on Windows
    # CI). So disable insert-triggered vacuums and push the dead-tuple
    # threshold out of reach globally; test_autovac gets a workable threshold
    # back via reloptions below.
    node = create_pg(
        "autovac",
        conf={
            "autovacuum_max_workers": 1,
            "autovacuum_worker_slots": 1,
            "autovacuum_max_parallel_workers": 2,
            "max_worker_processes": 10,
            "max_parallel_workers": 10,
            "log_min_messages": "debug2",
            "autovacuum_naptime": "1s",
            "min_parallel_index_scan_size": 0,
            "log_autovacuum_min_duration": -1,
            "autovacuum_vacuum_insert_threshold": -1,
            "autovacuum_vacuum_threshold": 1000000,
        },
    )
    node.sql("CREATE EXTENSION injection_points")

    node.sql(
        "CREATE TABLE test_autovac ("
        "  id SERIAL PRIMARY KEY,"
        "  col_1 INTEGER, col_2 INTEGER, col_3 INTEGER, col_4 INTEGER"
        ") WITH (autovacuum_parallel_workers = 2, log_autovacuum_min_duration = 0,"
        "  autovacuum_vacuum_threshold = 50)"
    )
    node.sql(
        "INSERT INTO test_autovac SELECT g, g + 1, g + 2, g + 3 "
        "FROM generate_series(1, 10000) AS g"
    )
    for i in (1, 2, 3):
        node.sql(f"CREATE INDEX idx_col_{i} ON test_autovac (col_{i})")

    def prepare(test_number):
        # Disable autovacuum on the table and generate dead tuples. Use a
        # one-shot connection: pgstat_report_stat() forces a flush on backend
        # exit, whereas on the shared cached connection this update's dead
        # tuple count could sit unflushed for up to PGSTAT_IDLE_INTERVAL,
        # stalling autovacuum from picking it up in time.
        node.sql_oneshot("ALTER TABLE test_autovac SET (autovacuum_enabled = false)")
        node.sql_oneshot(f"UPDATE test_autovac SET col_1 = {test_number}")

    # Test 1: the table can be autovacuumed in parallel.
    prepare(1)
    offset = node.current_log_position()
    node.sql("ALTER TABLE test_autovac SET (autovacuum_enabled = true)")
    node.wait_for_log(
        r"parallel workers: index vacuum: 2 planned, 2 launched in total", offset
    )

    # Test 2: the leader propagates cost-based parameters to parallel workers.
    prepare(2)
    offset = node.current_log_position()
    node.sql(f"SELECT injection_points_attach('{POINT}', 'wait')")
    node.sql(
        "ALTER TABLE test_autovac "
        "SET (autovacuum_parallel_workers = 1, autovacuum_enabled = true)"
    )
    # Wait until the parallel autovacuum is initialized.
    node.wait_for_event("autovacuum worker", POINT)

    # Update the shared cost-based delay parameters. These ALTER SYSTEM
    # statements and the reload all run on one server with no bounce, and the
    # new values are consumed by the autovacuum worker rather than this session.
    node.sql("ALTER SYSTEM SET autovacuum_vacuum_cost_limit = 500")
    node.sql("ALTER SYSTEM SET autovacuum_vacuum_cost_delay = 5")
    node.sql("ALTER SYSTEM SET vacuum_cost_page_miss = 10")
    node.sql("ALTER SYSTEM SET vacuum_cost_page_dirty = 10")
    node.sql("ALTER SYSTEM SET vacuum_cost_page_hit = 10")
    node.sql("SELECT pg_reload_conf()")

    # Resume the leader; it updates the shared params during heap scan and
    # launches a worker, which picks them up while processing indexes.
    node.sql(f"SELECT injection_points_wakeup('{POINT}')")
    node.wait_for_log(
        r"parallel autovacuum worker updated cost params: cost_limit=500, "
        r"cost_delay=5, cost_page_miss=10, cost_page_dirty=10, cost_page_hit=10",
        offset,
    )

    node.sql(f"SELECT injection_points_detach('{POINT}')")
    node.stop()
