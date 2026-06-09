# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/032_relfilenode_reuse.pl.

Exercises relfilenode reuse caused by database OID reuse (DROP + CREATE DATABASE
with the same OID) and by moving a database in and out of a tablespace. Long-
running transactions keep file descriptors open and pg_prewarm forces buffer
eviction / write-back, so the test detects a backend confusing an old file with
a new one that reused its relfilenode. Updates must not be lost and the standby
must stay consistent.
"""

import re

from pypg.bins import pg_controldata

EVICT = (
    "SELECT SUM(pg_prewarm(oid)) warmed_buffers FROM pg_class "
    "WHERE pg_relation_filenode(oid) != 0"
)
GROUP_QUERY = "SELECT datab, count(*) FROM large GROUP BY 1 ORDER BY 1 LIMIT 10"


def test_relfilenode_reuse(create_pg):
    primary = create_pg(
        "primary",
        allows_streaming=True,
        conf={
            "allow_in_place_tablespaces": True,
            "log_connections": "receipt",
            "full_page_writes": False,  # to avoid "repairing" corruption
            "log_min_messages": "debug2",
            "shared_buffers": "1MB",
        },
    )
    backup = primary.backup("my_backup")
    standby = create_pg("standby", from_backup=backup, streaming_primary=primary)

    # A template database with preexisting rows makes reuse easier to reproduce
    # (no cache invalidations), using explicit OIDs to force the conflict.
    primary.sql("CREATE DATABASE conflict_db_template OID = 50000")
    primary.sql_batch_oneshot(
        "CREATE TABLE large(id serial primary key, dataa text, datab text)",
        "INSERT INTO large(dataa, datab) "
        "SELECT g.i::text, 1 FROM generate_series(1, 4000) g(i)",
        dbname="conflict_db_template",
    )
    primary.sql("CREATE DATABASE conflict_db TEMPLATE conflict_db_template OID = 50001")

    primary.sql_batch(
        "CREATE EXTENSION pg_prewarm",
        "CREATE TABLE replace_sb(data text)",
        "INSERT INTO replace_sb(data) SELECT random()::text FROM generate_series(1, 15000)",
    )
    primary.wait_for_catchup(standby)

    # Long-running transactions so AtEOXact_SMgr does not close the files.
    psql_primary = primary.connect()
    psql_standby = standby.connect()
    psql_primary.sql("BEGIN")
    psql_standby.sql("BEGIN")

    def cause_eviction():
        # Forces write-back of dirty data, opening the relevant file descriptors
        # inside the held transactions.
        psql_primary.sql(EVICT)
        psql_standby.sql(EVICT)

    def verify(counter, message):
        assert primary.sql_oneshot(GROUP_QUERY, dbname="conflict_db") == (
            str(counter),
            4000,
        ), f"primary: {message}"
        primary.wait_for_catchup(standby)
        assert standby.sql_oneshot(GROUP_QUERY, dbname="conflict_db") == (
            str(counter),
            4000,
        ), f"standby: {message}"

    # Dirty lots of rows, then do work in another database to write them back.
    primary.sql_oneshot("UPDATE large SET datab = 1", dbname="conflict_db")
    cause_eviction()

    # Drop and recreate the database, reusing OID 50001.
    primary.sql("DROP DATABASE conflict_db")
    primary.sql("CREATE DATABASE conflict_db TEMPLATE conflict_db_template OID = 50001")
    verify(1, "initial contents as expected")

    primary.sql_oneshot("UPDATE large SET datab = 2", dbname="conflict_db")
    cause_eviction()
    verify(2, "update to reused relfilenode (due to DB oid conflict) is not lost")

    primary.sql_oneshot("VACUUM FULL large", dbname="conflict_db")
    primary.sql_oneshot("UPDATE large SET datab = 3", dbname="conflict_db")
    verify(3, "restored contents as expected")

    # Old filehandles after moving a database in/out of a tablespace.
    primary.sql("CREATE TABLESPACE test_tablespace LOCATION ''")
    primary.sql_oneshot("UPDATE large SET datab = 4", dbname="conflict_db")
    cause_eviction()
    primary.sql("ALTER DATABASE conflict_db SET TABLESPACE test_tablespace")
    primary.sql("ALTER DATABASE conflict_db SET TABLESPACE pg_default")
    primary.sql_oneshot("UPDATE large SET datab = 5", dbname="conflict_db")
    cause_eviction()
    verify(5, "post move contents as expected")

    primary.sql("ALTER DATABASE conflict_db SET TABLESPACE test_tablespace")
    primary.sql_oneshot("UPDATE large SET datab = 7", dbname="conflict_db")
    cause_eviction()
    primary.sql_oneshot("UPDATE large SET datab = 8", dbname="conflict_db")
    primary.sql("DROP DATABASE conflict_db")
    primary.sql("DROP TABLESPACE test_tablespace")
    primary.sql("REINDEX TABLE pg_database")

    psql_primary.close()
    psql_standby.close()
    primary.stop()
    standby.stop()

    # No crashes during shutdown: control files show clean shutdown states.
    assert re.search(
        r"Database cluster state:\s+shut down\n",
        pg_controldata.capture(primary.datadir),
    ), "primary shut down ok"
    assert re.search(
        r"Database cluster state:\s+shut down in recovery\n",
        pg_controldata.capture(standby.datadir),
    ), "standby shut down ok"
