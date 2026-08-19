# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/033_replay_tsp_drops.pl.

Tests replay of tablespace and database creation/drop. A burst of CREATE/DROP
DATABASE and DROP TABLESPACE followed by an immediate standby shutdown forces
CREATE DATABASE WAL to be applied against already-removed directories; the
standby must restart, ignoring the directory-creation errors. Run for both the
FILE_COPY and WAL_LOG strategies. Finally, a missing tablespace directory on a
standby that has already reached consistency must be reported (the PANIC is
downgraded to a WARNING by allow_in_place_tablespaces).
"""

import pathlib
import shutil


def test_replay_tsp_drops(create_pg):
    for strategy in ("FILE_COPY", "WAL_LOG"):
        _test_tablespace(create_pg, strategy)

    _test_missing_tablespace_dir(create_pg)


def _test_tablespace(create_pg, strategy):
    primary = create_pg(f"primary1_{strategy}", allows_streaming=True)

    # In-place tablespaces (empty LOCATION) require the GUC on the creating
    # session; each CREATE TABLESPACE/DATABASE is its own statement as they
    # cannot run inside a transaction block.
    primary.sql("SET allow_in_place_tablespaces = on")
    for ts in ("dropme_ts1", "dropme_ts2", "source_ts", "target_ts"):
        primary.sql(f"CREATE TABLESPACE {ts} LOCATION ''")
    primary.sql("CREATE DATABASE template_db IS_TEMPLATE = true")
    primary.sql("SELECT pg_create_physical_replication_slot('slot', true)")

    backup = primary.backup("my_backup")
    standby = create_pg(
        f"standby2_{strategy}",
        from_backup=backup,
        streaming_primary=primary,
        conf={"allow_in_place_tablespaces": True, "primary_slot_name": "slot"},
    )
    primary.wait_for_catchup(standby, "write")

    # CREATE DATABASE / DROP DATABASE / DROP TABLESPACE just before an immediate
    # shutdown, so CREATE DATABASE WAL is applied to already-removed directories.
    statements = [
        f"CREATE DATABASE dropme_db1 WITH TABLESPACE dropme_ts1 STRATEGY={strategy}",
        "CREATE TABLE t (a int) TABLESPACE dropme_ts2",
        f"CREATE DATABASE dropme_db2 WITH TABLESPACE dropme_ts2 STRATEGY={strategy}",
        f"CREATE DATABASE moveme_db TABLESPACE source_ts STRATEGY={strategy}",
        "ALTER DATABASE moveme_db SET TABLESPACE target_ts",
        f"CREATE DATABASE newdb TEMPLATE template_db STRATEGY={strategy}",
        "ALTER DATABASE template_db IS_TEMPLATE = false",
        "DROP DATABASE dropme_db1",
        "DROP TABLE t",
        "DROP DATABASE dropme_db2",
        "DROP TABLESPACE dropme_ts2",
        "DROP TABLESPACE source_ts",
        "DROP DATABASE template_db",
    ]
    for stmt in statements:
        primary.sql(stmt)
    primary.wait_for_catchup(standby, "write")

    standby.sql("ALTER SYSTEM SET log_min_messages TO debug1")
    standby.stop("immediate")
    # The standby must restart, ignoring the directory-creation errors.
    standby.start()
    standby.stop("immediate")


def _test_missing_tablespace_dir(create_pg):
    # A missing tablespace directory during CREATE DATABASE replay must panic
    # immediately once the standby has reached consistency (archive recovery).
    # With allow_in_place_tablespaces the PANIC is downgraded to a WARNING, so
    # the log is checked instead of a crash. Only effective for FILE_COPY.
    primary = create_pg("primary2", allows_streaming=True)
    primary.sql("SET allow_in_place_tablespaces = on")
    primary.sql("CREATE TABLESPACE ts1 LOCATION ''")
    primary.sql("CREATE DATABASE db1 WITH TABLESPACE ts1 STRATEGY=FILE_COPY")

    backup = primary.backup("my_backup")
    standby = create_pg(
        "standby3",
        from_backup=backup,
        streaming_primary=primary,
        conf={"allow_in_place_tablespaces": True},
    )
    # Make sure the standby reached consistency and accepts connections.
    standby.poll_query_until("SELECT 1", expected=1)

    # Remove the standby's tablespace directory so it is missing on replay.
    tspoid = standby.sql("SELECT oid FROM pg_tablespace WHERE spcname = 'ts1'")
    tspdir = pathlib.Path(standby.datadir) / "pg_tblspc" / str(tspoid)
    shutil.rmtree(tspdir)

    logstart = standby.current_log_position()

    # Create a database in the tablespace plus a table in the default one; the
    # standby must not silently skip replaying this WAL.
    primary.sql("CREATE TABLE should_not_replay_insertion(a int)")
    primary.sql("CREATE DATABASE db2 WITH TABLESPACE ts1 STRATEGY=FILE_COPY")
    primary.sql("INSERT INTO should_not_replay_insertion VALUES (1)")

    # The missing directory must be detected (wait_for_log raises on timeout).
    standby.wait_for_log("creating missing directory: pg_tblspc/", logstart)
