# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/030_stats_cleanup_replica.pl.

Tests that a standby drops object statistics when the drop is replayed
(directly, via DROP SCHEMA CASCADE, and via DROP DATABASE), persists stats
across a graceful restart, and discards them after an immediate (crash)
restart.

pg_stat_have_stats() looks stats up by OID across databases, so every status
check runs on a single standby connection to 'postgres'; only object creation
and stat generation need a connection to the database under test.
"""


def test_stats_cleanup_replica(create_pg):
    primary = create_pg(
        "primary", allows_streaming=True, conf={"track_functions": "all"}
    )
    backup = primary.backup("my_backup")
    standby = create_pg("standby", from_backup=backup, streaming_primary=primary)

    def have_stats(kind, dboid, objid):
        return standby.sql(f"SELECT pg_stat_have_stats('{kind}', {dboid}, {objid})")

    def check_func_tab(dboid, tableoid, funcoid, present):
        assert have_stats("relation", dboid, tableoid) is present, (
            "standby relation stats as expected"
        )
        assert have_stats("function", dboid, funcoid) is present, (
            "standby function stats as expected"
        )

    def populate(p_conn, schema, dbname):
        """Create a table and function on the primary, then generate stats for
        them on the standby; return (dboid, tableoid, funcoid).

        The standby stats are generated on a short-lived connection that then
        disconnects, flushing its pending stats — like the Perl test's per-query
        psql. A long-lived connection would keep the relation's stats pending
        and re-create the shared entry after the drop is replayed, defeating the
        cleanup the test checks for.
        """
        p_conn.sql(
            f"CREATE TABLE {schema}.drop_tab_test1 AS SELECT generate_series(1,100) AS a"
        )
        p_conn.sql(
            f"CREATE FUNCTION {schema}.drop_func_test1() RETURNS VOID AS 'select 2;' "
            "LANGUAGE SQL IMMUTABLE"
        )
        primary.wait_for_catchup(standby)

        with standby.connect(dbname=dbname) as gen:
            dboid = gen.sql(f"SELECT oid FROM pg_database WHERE datname = '{dbname}'")
            tableoid = gen.sql(f"SELECT '{schema}.drop_tab_test1'::regclass::oid")
            funcoid = gen.sql(f"SELECT '{schema}.drop_func_test1()'::regprocedure::oid")
            gen.sql(f"SELECT * FROM {schema}.drop_tab_test1")
            gen.sql(f"SELECT {schema}.drop_func_test1()")
        return dboid, tableoid, funcoid

    # Stats are cleaned up on the standby after the table/function are dropped.
    dboid, tableoid, funcoid = populate(primary, "public", "postgres")
    check_func_tab(dboid, tableoid, funcoid, True)

    primary.sql(f"DROP TABLE {primary.sql(f'SELECT {tableoid}::regclass')}")
    primary.sql(f"DROP FUNCTION {primary.sql(f'SELECT {funcoid}::regprocedure')}")
    primary.wait_for_catchup(standby)
    check_func_tab(dboid, tableoid, funcoid, False)

    # Cleaned up after an indirect drop via DROP SCHEMA CASCADE.
    primary.sql("CREATE SCHEMA drop_schema_test1")
    primary.wait_for_catchup(standby)
    dboid, tableoid, funcoid = populate(primary, "drop_schema_test1", "postgres")
    check_func_tab(dboid, tableoid, funcoid, True)
    primary.sql("DROP SCHEMA drop_schema_test1 CASCADE")
    primary.wait_for_catchup(standby)
    check_func_tab(dboid, tableoid, funcoid, False)

    # Cleaned up after dropping the whole database.
    primary.sql("CREATE DATABASE test")
    primary.wait_for_catchup(standby)
    with primary.connect(dbname="test") as ptconn:
        dboid, tableoid, funcoid = populate(ptconn, "public", "test")
        check_func_tab(dboid, tableoid, funcoid, True)
        assert have_stats("database", dboid, 0) is True, "standby db stats present"
    primary.sql("DROP DATABASE test")
    primary.wait_for_catchup(standby)
    # Checked from 'postgres' using the dropped database's OID acquired above.
    check_func_tab(dboid, tableoid, funcoid, False)
    assert have_stats("database", dboid, 0) is False, "standby db stats removed"

    # Stats persist across a graceful restart of the replica. (Database stats
    # can't be tested here, they are repopulated immediately on reconnect.)
    dboid, tableoid, funcoid = populate(primary, "public", "postgres")
    check_func_tab(dboid, tableoid, funcoid, True)

    standby.pg_ctl("restart")
    check_func_tab(dboid, tableoid, funcoid, True)

    # But are gone after an immediate (crash) restart.
    standby.stop("immediate")
    standby.start()
    check_func_tab(dboid, tableoid, funcoid, False)
