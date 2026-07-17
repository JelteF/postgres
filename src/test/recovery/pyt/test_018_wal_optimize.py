# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/018_wal_optimize.pl.

Tests WAL replay when an operation skipped WAL (the "Skipping WAL for New
RelFileLocator" optimization). Each case commits a transaction, performs an
immediate (crash) shutdown, restarts, and confirms the expected data survives.
COPY and TRUNCATE appear frequently because individual commands historically
decided whether to skip WAL. Run for both wal_level=minimal and replica.
"""

import os
import pathlib

import pytest

from libpq import LibpqError


def check_orphan_relfilenodes(conn, datadir, test_name):
    """Every numeric relation file in base/<db> must be referenced by a
    non-temp relation in pg_class, i.e. no orphan relfilenodes remain."""
    db_oid = conn.sql("SELECT oid FROM pg_database WHERE datname = 'postgres'")
    prefix = f"base/{db_oid}/"
    referenced = conn.sql(
        "SELECT pg_relation_filepath(oid) FROM pg_class "
        "WHERE reltablespace = 0 AND relpersistence <> 't' AND "
        "pg_relation_filepath(oid) IS NOT NULL"
    )
    on_disk = sorted(
        prefix + f for f in os.listdir(pathlib.Path(datadir) / prefix) if f.isdigit()
    )
    assert on_disk == sorted(referenced), test_name


@pytest.mark.parametrize("wal_level", ["minimal", "replica"])
def test_wal_optimize(create_pg, wal_level):
    conf = {
        "wal_level": wal_level,
        "max_prepared_transactions": 1,
        "wal_log_hints": True,
        "wal_skip_threshold": 0,
    }
    # wal_level = minimal requires no WAL senders.
    if wal_level == "minimal":
        conf["max_wal_senders"] = 0
    node = create_pg(f"node_{wal_level}", conf=conf)

    # Put the tablespace directory under the data directory's parent rather
    # than pytest's tmp_path: on Windows the (privilege-dropped) postmaster
    # must be able to set permissions on it, and the CI grants the needed ACLs
    # on the test tree but not on the system temp directory.
    tablespace_dir = node.datadir.parent / f"tablespace_other_{wal_level}"
    tablespace_dir.mkdir()

    # A data file for COPY in several cases below. Server-side COPY reads it as
    # the (privilege-dropped on Windows) backend, so it must live under the test
    # data tree the CI grants ACLs on, not pytest's tmp_path under the system
    # temp directory.
    copy_file = node.datadir.parent / f"copy_data_{wal_level}.txt"
    copy_file.write_text("20000,30000\n20001,30001\n20002,30002\n")

    def crash_and_restart():
        node.stop("immediate")
        node.start()

    # Redo of CREATE TABLESPACE. CREATE TABLESPACE cannot run in a transaction
    # block, so the statements before BEGIN are issued separately.
    node.sql("CREATE TABLE moved (id int)")
    node.sql("INSERT INTO moved VALUES (1)")
    node.sql(f"CREATE TABLESPACE other LOCATION '{tablespace_dir}'")
    node.sql_batch(
        "BEGIN",
        "ALTER TABLE moved SET TABLESPACE other",
        "CREATE TABLE originated (id int)",
        "INSERT INTO originated VALUES (1)",
        "CREATE UNIQUE INDEX ON originated(id) TABLESPACE other",
        "COMMIT",
    )
    crash_and_restart()
    assert node.sql("SELECT count(*) FROM moved") == 1, (
        f"wal_level = {wal_level}, CREATE+SET TABLESPACE"
    )
    assert (
        node.sql(
            "INSERT INTO originated VALUES (1) ON CONFLICT (id) "
            "DO UPDATE set id = originated.id + 1 RETURNING id"
        )
        == 2
    ), f"wal_level = {wal_level}, CREATE TABLESPACE, CREATE INDEX"

    # Direct truncation optimization, no tuples.
    node.sql_batch(
        "BEGIN",
        "CREATE TABLE trunc (id serial PRIMARY KEY)",
        "TRUNCATE trunc",
        "COMMIT",
    )
    crash_and_restart()
    assert node.sql("SELECT count(*) FROM trunc") == 0, (
        f"wal_level = {wal_level}, TRUNCATE with empty table"
    )

    # Truncation with tuples inserted after the truncation in the same xact.
    node.sql_batch(
        "BEGIN",
        "CREATE TABLE trunc_ins (id serial PRIMARY KEY)",
        "INSERT INTO trunc_ins VALUES (DEFAULT)",
        "TRUNCATE trunc_ins",
        "INSERT INTO trunc_ins VALUES (DEFAULT)",
        "COMMIT",
    )
    crash_and_restart()
    assert node.sql("SELECT count(*), min(id) FROM trunc_ins") == (1, 2), (
        f"wal_level = {wal_level}, TRUNCATE INSERT"
    )

    # Same for a prepared transaction.
    node.sql_batch(
        "BEGIN",
        "CREATE TABLE twophase (id serial PRIMARY KEY)",
        "INSERT INTO twophase VALUES (DEFAULT)",
        "TRUNCATE twophase",
        "INSERT INTO twophase VALUES (DEFAULT)",
        "PREPARE TRANSACTION 't'",
    )
    node.sql("COMMIT PREPARED 't'")
    crash_and_restart()
    assert node.sql("SELECT count(*), min(id) FROM trunc_ins") == (1, 2), (
        f"wal_level = {wal_level}, TRUNCATE INSERT PREPARE"
    )

    # Writing WAL at end of xact instead of syncing. wal_skip_threshold must be
    # set on the same session that runs the transaction.
    with node.connect() as c:
        c.sql("SET wal_skip_threshold = '1GB'")
        c.sql_batch(
            "BEGIN",
            "CREATE TABLE noskip (id serial PRIMARY KEY)",
            "INSERT INTO noskip (SELECT FROM generate_series(1, 20000) a)",
            "COMMIT",
        )
    crash_and_restart()
    assert node.sql("SELECT count(*) FROM noskip") == 20000, (
        f"wal_level = {wal_level}, end-of-xact WAL"
    )

    # Truncation with tuples inserted via both INSERT and COPY after truncation.
    node.sql_batch(
        "BEGIN",
        "CREATE TABLE ins_trunc (id serial PRIMARY KEY, id2 int)",
        "INSERT INTO ins_trunc VALUES (DEFAULT, generate_series(1,10000))",
        "TRUNCATE ins_trunc",
        "INSERT INTO ins_trunc (id, id2) VALUES (DEFAULT, 10000)",
        f"COPY ins_trunc FROM '{copy_file}' DELIMITER ','",
        "INSERT INTO ins_trunc (id, id2) VALUES (DEFAULT, 10000)",
        "COMMIT",
    )
    crash_and_restart()
    assert node.sql("SELECT count(*) FROM ins_trunc") == 5, (
        f"wal_level = {wal_level}, TRUNCATE COPY INSERT"
    )

    # Truncation with tuples copied after the truncation.
    node.sql_batch(
        "BEGIN",
        "CREATE TABLE trunc_copy (id serial PRIMARY KEY, id2 int)",
        "INSERT INTO trunc_copy VALUES (DEFAULT, generate_series(1,3000))",
        "TRUNCATE trunc_copy",
        f"COPY trunc_copy FROM '{copy_file}' DELIMITER ','",
        "COMMIT",
    )
    crash_and_restart()
    assert node.sql("SELECT count(*) FROM trunc_copy") == 3, (
        f"wal_level = {wal_level}, TRUNCATE COPY"
    )

    # Rollback SET TABLESPACE in a subtransaction.
    node.sql_batch(
        "BEGIN",
        "CREATE TABLE spc_abort (id serial PRIMARY KEY, id2 int)",
        "INSERT INTO spc_abort VALUES (DEFAULT, generate_series(1,3000))",
        "TRUNCATE spc_abort",
        "SAVEPOINT s",
        "ALTER TABLE spc_abort SET TABLESPACE other",
        "ROLLBACK TO s",
        f"COPY spc_abort FROM '{copy_file}' DELIMITER ','",
        "COMMIT",
    )
    crash_and_restart()
    assert node.sql("SELECT count(*) FROM spc_abort") == 3, (
        f"wal_level = {wal_level}, SET TABLESPACE abort subtransaction"
    )

    # Commit SET TABLESPACE in a subtransaction.
    node.sql_batch(
        "BEGIN",
        "CREATE TABLE spc_commit (id serial PRIMARY KEY, id2 int)",
        "INSERT INTO spc_commit VALUES (DEFAULT, generate_series(1,3000))",
        "TRUNCATE spc_commit",
        "SAVEPOINT s",
        "ALTER TABLE spc_commit SET TABLESPACE other",
        "RELEASE s",
        f"COPY spc_commit FROM '{copy_file}' DELIMITER ','",
        "COMMIT",
    )
    crash_and_restart()
    assert node.sql("SELECT count(*) FROM spc_commit") == 3, (
        f"wal_level = {wal_level}, SET TABLESPACE commit subtransaction"
    )

    # Nested subtransactions around SET TABLESPACE.
    node.sql_batch(
        "BEGIN",
        "CREATE TABLE spc_nest (id serial PRIMARY KEY, id2 int)",
        "INSERT INTO spc_nest VALUES (DEFAULT, generate_series(1,3000))",
        "TRUNCATE spc_nest",
        "SAVEPOINT s",
        "ALTER TABLE spc_nest SET TABLESPACE other",
        "SAVEPOINT s2",
        "ALTER TABLE spc_nest SET TABLESPACE pg_default",
        "ROLLBACK TO s2",
        "SAVEPOINT s2",
        "ALTER TABLE spc_nest SET TABLESPACE pg_default",
        "RELEASE s2",
        "ROLLBACK TO s",
        f"COPY spc_nest FROM '{copy_file}' DELIMITER ','",
        "COMMIT",
    )
    crash_and_restart()
    assert node.sql("SELECT count(*) FROM spc_nest") == 3, (
        f"wal_level = {wal_level}, SET TABLESPACE nested subtransaction"
    )

    # SET TABLESPACE with a hint bit set mid-transaction.
    node.sql_batch(
        "CREATE TABLE spc_hint (id int)",
        "INSERT INTO spc_hint VALUES (1)",
        "BEGIN",
        "ALTER TABLE spc_hint SET TABLESPACE other",
        "CHECKPOINT",
        "SELECT * FROM spc_hint",
        "INSERT INTO spc_hint VALUES (2)",
        "COMMIT",
    )
    crash_and_restart()
    assert node.sql("SELECT count(*) FROM spc_hint") == 2, (
        f"wal_level = {wal_level}, SET TABLESPACE, hint bit"
    )

    # Unique index LP_DEAD hint bit handling.
    node.sql_batch(
        "BEGIN",
        "CREATE TABLE idx_hint (c int PRIMARY KEY)",
        "SAVEPOINT q",
        "INSERT INTO idx_hint VALUES (1)",
        "ROLLBACK TO q",
        "CHECKPOINT",
        "INSERT INTO idx_hint VALUES (1)",
        "INSERT INTO idx_hint VALUES (2)",
        "COMMIT",
    )
    crash_and_restart()
    with pytest.raises(LibpqError, match="violates unique"):
        node.sql("INSERT INTO idx_hint VALUES (2)")

    # UPDATE touches two buffers for one row.
    node.sql_batch(
        "BEGIN",
        "CREATE TABLE upd (id serial PRIMARY KEY, id2 int)",
        "INSERT INTO upd (id, id2) VALUES (DEFAULT, generate_series(1,10000))",
        f"COPY upd FROM '{copy_file}' DELIMITER ','",
        "UPDATE upd SET id2 = id2 + 1",
        "DELETE FROM upd",
        "COMMIT",
    )
    crash_and_restart()
    assert node.sql("SELECT count(*) FROM upd") == 0, (
        f"wal_level = {wal_level}, UPDATE touches two buffers for one row"
    )

    # COPY with INSERT for a table created in the same transaction.
    node.sql_batch(
        "BEGIN",
        "CREATE TABLE ins_copy (id serial PRIMARY KEY, id2 int)",
        "INSERT INTO ins_copy VALUES (DEFAULT, 1)",
        f"COPY ins_copy FROM '{copy_file}' DELIMITER ','",
        "COMMIT",
    )
    crash_and_restart()
    assert node.sql("SELECT count(*) FROM ins_copy") == 4, (
        f"wal_level = {wal_level}, INSERT COPY"
    )

    # COPY that inserts more rows into the same table via triggers.
    node.sql_batch(
        "BEGIN",
        "CREATE TABLE ins_trig (id serial PRIMARY KEY, id2 text)",
        "CREATE FUNCTION ins_trig_before_row_trig() RETURNS trigger"
        "   LANGUAGE plpgsql as $$"
        "   BEGIN"
        "     IF new.id2 NOT LIKE 'triggered%' THEN"
        "       INSERT INTO ins_trig"
        "         VALUES (DEFAULT, 'triggered row before' || NEW.id2);"
        "     END IF;"
        "     RETURN NEW;"
        "   END; $$",
        "CREATE FUNCTION ins_trig_after_row_trig() RETURNS trigger"
        "   LANGUAGE plpgsql as $$"
        "   BEGIN"
        "     IF new.id2 NOT LIKE 'triggered%' THEN"
        "       INSERT INTO ins_trig"
        "         VALUES (DEFAULT, 'triggered row after' || NEW.id2);"
        "     END IF;"
        "     RETURN NEW;"
        "   END; $$",
        "CREATE TRIGGER ins_trig_before_row_insert"
        "   BEFORE INSERT ON ins_trig"
        "   FOR EACH ROW EXECUTE PROCEDURE ins_trig_before_row_trig()",
        "CREATE TRIGGER ins_trig_after_row_insert"
        "   AFTER INSERT ON ins_trig"
        "   FOR EACH ROW EXECUTE PROCEDURE ins_trig_after_row_trig()",
        f"COPY ins_trig FROM '{copy_file}' DELIMITER ','",
        "COMMIT",
    )
    crash_and_restart()
    assert node.sql("SELECT count(*) FROM ins_trig") == 9, (
        f"wal_level = {wal_level}, COPY with INSERT triggers"
    )

    # INSERT, COPY and TRUNCATE with TRUNCATE triggers.
    node.sql_batch(
        "BEGIN",
        "CREATE TABLE trunc_trig (id serial PRIMARY KEY, id2 text)",
        "CREATE FUNCTION trunc_trig_before_stat_trig() RETURNS trigger"
        "   LANGUAGE plpgsql as $$"
        "   BEGIN"
        "     INSERT INTO trunc_trig VALUES (DEFAULT, 'triggered stat before');"
        "     RETURN NULL;"
        "   END; $$",
        "CREATE FUNCTION trunc_trig_after_stat_trig() RETURNS trigger"
        "   LANGUAGE plpgsql as $$"
        "   BEGIN"
        "     INSERT INTO trunc_trig VALUES (DEFAULT, 'triggered stat before');"
        "     RETURN NULL;"
        "   END; $$",
        "CREATE TRIGGER trunc_trig_before_stat_truncate"
        "   BEFORE TRUNCATE ON trunc_trig"
        "   FOR EACH STATEMENT EXECUTE PROCEDURE trunc_trig_before_stat_trig()",
        "CREATE TRIGGER trunc_trig_after_stat_truncate"
        "   AFTER TRUNCATE ON trunc_trig"
        "   FOR EACH STATEMENT EXECUTE PROCEDURE trunc_trig_after_stat_trig()",
        "INSERT INTO trunc_trig VALUES (DEFAULT, 1)",
        "TRUNCATE trunc_trig",
        f"COPY trunc_trig FROM '{copy_file}' DELIMITER ','",
        "COMMIT",
    )
    crash_and_restart()
    assert node.sql("SELECT count(*) FROM trunc_trig") == 4, (
        f"wal_level = {wal_level}, TRUNCATE COPY with TRUNCATE triggers"
    )

    # Redo of temp table creation leaves no orphan relfilenode. Use a scoped
    # connection that closes (dropping the temp table) before the crash, as the
    # Perl test's psql process did.
    with node.connect() as temp_conn:
        temp_conn.sql("CREATE TEMP TABLE temp (id serial PRIMARY KEY, id2 text)")
    crash_and_restart()
    check_orphan_relfilenodes(
        node, node.datadir, f"wal_level = {wal_level}, no orphan relfilenode remains"
    )
