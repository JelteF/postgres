# Copyright (c) 2023-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/worker_spi/t/001_worker_spi.pl."""

import contextlib

from libpq import LibpqError


def test_worker_spi(create_pg):
    node = create_pg("mynode")

    # --- dynamic bgworkers ---
    node.sql("CREATE EXTENSION worker_spi;")

    # Launch one dynamic worker and wait for its initialization to complete:
    # it creates a table "counted" in a schema named for the launch argument.
    assert node.sql("SELECT worker_spi_launch(4) IS NOT NULL")
    node.poll_query_until(
        "SELECT count(*) > 0 FROM information_schema.tables"
        " WHERE table_schema = 'schema4' AND table_name = 'counted';"
    )

    node.sql("INSERT INTO schema4.counted VALUES ('total', 0), ('delta', 1);")
    # A SIGHUP forces the worker to loop once, accelerating the test.
    node.pg_ctl("reload")
    # Wait until the worker has processed the tuple just inserted.
    node.poll_query_until(
        "SELECT count(*) FROM schema4.counted WHERE type = 'delta';", expected=0
    )
    assert node.sql("SELECT * FROM schema4.counted;") == ("total", 1)

    # The dynamic bgworker reports its wait event.
    node.poll_query_until(
        "SELECT wait_event FROM pg_stat_activity WHERE backend_type ~ 'worker_spi';",
        expected="WorkerSpiMain",
    )
    assert node.sql(
        "SELECT count(*) > 0 FROM pg_wait_events"
        " WHERE type = 'Extension' AND name = 'WorkerSpiMain';"
    )

    # --- bgworkers loaded with shared_preload_libraries ---
    # Create the database first so the workers can connect when the library loads.
    node.sql("CREATE DATABASE mydb;")
    node.sql("CREATE ROLE myrole SUPERUSER LOGIN;")
    node.connect(dbname="mydb").sql("CREATE EXTENSION worker_spi;")

    node.append_conf(
        **{
            "shared_preload_libraries": "worker_spi",
            "worker_spi.database": "mydb",
            "worker_spi.total_workers": 3,
            "max_worker_processes": 32,
        }
    )
    node.pg_ctl("restart")

    mydb = node.connect(dbname="mydb")

    # The preloaded workers have been registered and launched.
    node.poll_query_until(
        "SELECT datname, count(datname), wait_event FROM pg_stat_activity"
        " WHERE backend_type = 'worker_spi' GROUP BY datname, wait_event;",
        expected=("mydb", 3, "WorkerSpiMain"),
        dbname="mydb",
    )

    # Launch dynamic bgworkers with the library loaded, using a new role on
    # different databases and IDs that don't overlap the earlier schemas.
    myrole_id = mydb.sql("SELECT oid FROM pg_roles WHERE rolname = 'myrole';")
    mydb_id = mydb.sql("SELECT oid FROM pg_database WHERE datname = 'mydb';")
    postgresdb_id = mydb.sql("SELECT oid FROM pg_database WHERE datname = 'postgres';")
    worker1_pid = mydb.sql(f"SELECT worker_spi_launch(10, {mydb_id}, {myrole_id});")
    worker2_pid = mydb.sql(
        f"SELECT worker_spi_launch(11, {postgresdb_id}, {myrole_id});"
    )

    node.poll_query_until(
        "SELECT datname, usename, wait_event FROM pg_stat_activity"
        " WHERE backend_type = 'worker_spi dynamic' AND"
        f" pid IN ({worker1_pid}, {worker2_pid}) ORDER BY datname;",
        expected=[
            ("mydb", "myrole", "WorkerSpiMain"),
            ("postgres", "myrole", "WorkerSpiMain"),
        ],
        dbname="mydb",
    )

    # Check BGWORKER_BYPASS_ALLOWCONN.
    node.sql("CREATE DATABASE noconndb ALLOW_CONNECTIONS false;")
    noconndb_id = mydb.sql("SELECT oid FROM pg_database WHERE datname = 'noconndb';")
    log_offset = node.current_log_position()
    # The launch may itself detect that the worker stopped, so tolerate an error.
    with contextlib.suppress(LibpqError):
        node.sql(f"SELECT worker_spi_launch(12, {noconndb_id}, {myrole_id});")
    node.wait_for_log(
        r'database "noconndb" is not currently accepting connections', log_offset
    )

    # The bgworker bypasses the connection check and can be launched.
    worker4_pid = node.sql(
        f"SELECT worker_spi_launch(12, {noconndb_id}, {myrole_id}, '{{\"ALLOWCONN\"}}');"
    )
    node.poll_query_until(
        "SELECT datname, usename, wait_event FROM pg_stat_activity"
        " WHERE backend_type = 'worker_spi dynamic' AND"
        f" pid IN ({worker4_pid}) ORDER BY datname;",
        expected=("noconndb", "myrole", "WorkerSpiMain"),
    )

    # Check BGWORKER_BYPASS_ROLELOGINCHECK with a role that cannot log in.
    node.sql_batch(
        "CREATE ROLE nologrole WITH NOLOGIN",
        "GRANT CREATE ON DATABASE mydb TO nologrole",
    )
    nologrole_id = mydb.sql("SELECT oid FROM pg_roles WHERE rolname = 'nologrole';")
    log_offset = node.current_log_position()
    with contextlib.suppress(LibpqError):
        node.sql(f"SELECT worker_spi_launch(13, {mydb_id}, {nologrole_id});")
    node.wait_for_log(r'role "nologrole" is not permitted to log in', log_offset)

    # The bgworker bypasses the login restriction and can be launched.
    worker5_pid = mydb.sql(
        f"SELECT worker_spi_launch(13, {mydb_id}, {nologrole_id}, '{{\"ROLELOGINCHECK\"}}');"
    )
    node.poll_query_until(
        "SELECT datname, usename, wait_event FROM pg_stat_activity"
        " WHERE backend_type = 'worker_spi dynamic' AND"
        f" pid = {worker5_pid};",
        expected=("mydb", "nologrole", "WorkerSpiMain"),
        dbname="mydb",
    )
