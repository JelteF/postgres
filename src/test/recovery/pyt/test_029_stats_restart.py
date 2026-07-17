# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/029_stats_restart.pl.

Tests statistics handling around restarts: a clean shutdown persists the stats
file and a normal restart restores it, an immediate (crash) shutdown or an
invalid/corrupt stats file causes stats to be discarded, and checkpoint/WAL
stats survive restarts while honouring pg_stat_reset_shared().
"""

import pathlib
import shutil


def test_stats_restart(create_pg, tmp_path):
    node = create_pg("primary", allows_streaming=True, conf={"track_functions": "all"})

    db_under_test = "test"

    # node.sql() runs on the default 'postgres' database, which is what the
    # stats queries need; 'test' is only touched by the workload below, since
    # connecting to it eagerly would create database stats and break the
    # "stats were discarded" checks. node.sql() reconnects automatically after
    # every (re)start, so no manual reconnect is needed.
    def have_stats(kind, dboid, objid):
        return node.sql(f"SELECT pg_stat_have_stats('{kind}', {dboid}, {objid})")

    def io_stats(context, obj, backend_type):
        row = node.sql(
            "SELECT writes, reads FROM pg_stat_io "
            f"WHERE context = '{context}' AND object = '{obj}' AND "
            f"backend_type = '{backend_type}'"
        )
        return {"writes": row[0], "reads": row[1]}

    def checkpoint_stats():
        count = node.sql("SELECT num_timed + num_requested FROM pg_stat_checkpointer")
        reset = node.sql("SELECT stats_reset FROM pg_stat_checkpointer")
        return {"count": count, "reset": reset}

    def wal_stats():
        records = node.sql("SELECT wal_records FROM pg_stat_wal")
        wal_bytes = node.sql("SELECT wal_bytes FROM pg_stat_wal")
        reset = node.sql("SELECT stats_reset FROM pg_stat_wal")
        return {"records": records, "bytes": wal_bytes, "reset": reset}

    def trigger_funcrel_stat():
        # A fresh connection each time: it must connect to 'test' (generating
        # the stats under test), and the previous one is dead after a restart.
        node.connect(dbname=db_under_test).sql_batch(
            "SELECT * FROM tab_stats_crash_discard_test1",
            "SELECT func_stats_crash_discard1()",
            "SELECT pg_stat_force_next_flush()",
        )

    # After a fresh start the standalone backend (initdb) has done WAL writes and
    # the startup process has done WAL reads.
    standalone = io_stats("init", "wal", "standalone backend")
    startup = io_stats("normal", "wal", "startup")
    assert standalone["writes"] > 0, "startup: increased standalone backend IO writes"
    assert startup["reads"] > 0, "startup: increased startup IO reads"

    node.sql(f"CREATE DATABASE {db_under_test}")
    dconn = node.connect(dbname=db_under_test)
    dconn.sql(
        "CREATE TABLE tab_stats_crash_discard_test1 AS "
        "SELECT generate_series(1,100) AS a"
    )
    dconn.sql(
        "CREATE FUNCTION func_stats_crash_discard1() RETURNS VOID AS 'select 2;' "
        "LANGUAGE SQL IMMUTABLE"
    )

    dboid = dconn.sql(f"SELECT oid FROM pg_database WHERE datname = '{db_under_test}'")
    funcoid = dconn.sql("SELECT 'func_stats_crash_discard1()'::regprocedure::oid")
    tableoid = dconn.sql("SELECT 'tab_stats_crash_discard_test1'::regclass::oid")
    del dconn  # done with the setup connection; the workload reconnects as needed

    trigger_funcrel_stat()

    assert have_stats("database", dboid, 0), "initial: db stats do exist"
    assert have_stats("function", dboid, funcoid), "initial: function stats do exist"
    assert have_stats("relation", dboid, tableoid), "initial: relation stats do exist"

    node.stop()

    # Back up the stats file written by the clean shutdown.
    statsfile = tmp_path / "discard_stats1"
    assert not statsfile.exists(), "backup statsfile cannot already exist"
    og_stats = pathlib.Path(node.datadir) / "pg_stat" / "pgstat.stat"
    assert og_stats.is_file(), "origin stats file must exist"
    shutil.copy(og_stats, statsfile)

    # A normal restart restores the stats.
    node.start()
    assert have_stats("database", dboid, 0), "copy: db stats do exist"
    assert have_stats("function", dboid, funcoid), "copy: function stats do exist"
    assert have_stats("relation", dboid, tableoid), "copy: relation stats do exist"

    node.stop("immediate")
    assert not og_stats.exists(), "no stats file should exist after immediate shutdown"

    # Put the old stats file back; a crash-restart must discard it.
    shutil.copy(statsfile, og_stats)
    node.start()
    assert not have_stats("database", dboid, 0), "post immediate: db stats do not exist"
    assert not have_stats("function", dboid, funcoid), (
        "post immediate: function stats do not exist"
    )
    assert not have_stats("relation", dboid, tableoid), (
        "post immediate: relation stats do not exist"
    )

    statsfile.unlink()

    trigger_funcrel_stat()
    assert have_stats("database", dboid, 0), "post immediate, new: db stats do exist"
    assert have_stats("function", dboid, funcoid), (
        "post immediate, new: function stats do exist"
    )
    assert have_stats("relation", dboid, tableoid), (
        "post immediate, new: relation stats do exist"
    )

    node.stop()

    # An invalid stats file is ignored on startup.
    og_stats.write_text("ZZZZZZZZZZZZZ")
    node.start()
    assert not have_stats("database", dboid, 0), (
        "invalid_overwrite: db stats do not exist"
    )
    assert not have_stats("function", dboid, funcoid), (
        "invalid_overwrite: function stats do not exist"
    )
    assert not have_stats("relation", dboid, tableoid), (
        "invalid_overwrite: relation stats do not exist"
    )

    # Valid contents followed by trailing garbage is also rejected.
    trigger_funcrel_stat()
    node.stop()
    with open(og_stats, "a") as f:
        f.write("XYZ")
    node.start()
    assert not have_stats("database", dboid, 0), "invalid_append: db stats do not exist"
    assert not have_stats("function", dboid, funcoid), (
        "invalid_append: function stats do not exist"
    )
    assert not have_stats("relation", dboid, tableoid), (
        "invalid_append: relation stats do not exist"
    )

    # Enough checkpoints to keep the post-reset checks race-free on slow machines.
    node.sql("CHECKPOINT")
    node.sql("CHECKPOINT")

    # Checkpoint and WAL stats increase across a restart, stats_reset unchanged.
    ckpt_start = checkpoint_stats()
    wal_start = wal_stats()
    node.pg_ctl("restart")

    ckpt_restart = checkpoint_stats()
    wal_restart = wal_stats()
    assert ckpt_start["count"] < ckpt_restart["count"], (
        "post restart: increased checkpoint count"
    )
    assert wal_start["records"] < wal_restart["records"], (
        "post restart: increased wal record count"
    )
    assert wal_start["bytes"] < wal_restart["bytes"], (
        "post restart: increased wal bytes"
    )
    assert ckpt_start["reset"] == ckpt_restart["reset"], (
        "post restart: checkpoint stats_reset equal"
    )
    assert wal_start["reset"] == wal_restart["reset"], (
        "post restart: wal stats_reset equal"
    )

    # Resetting checkpointer stats does not affect WAL stats.
    node.sql("SELECT pg_stat_reset_shared('checkpointer')")
    ckpt_reset = checkpoint_stats()
    wal_ckpt_reset = wal_stats()
    assert ckpt_restart["count"] > ckpt_reset["count"], (
        "post ckpt reset: checkpoint count smaller"
    )
    assert ckpt_start["reset"] < ckpt_reset["reset"], (
        "post ckpt reset: stats_reset newer"
    )
    assert wal_restart["records"] <= wal_ckpt_reset["records"], (
        "post ckpt reset: wal record count not affected by reset"
    )
    assert wal_start["reset"] == wal_ckpt_reset["reset"], (
        "post ckpt reset: wal stats_reset equal"
    )

    # Checkpoint stats stay reset across a restart.
    node.pg_ctl("restart")
    ckpt_restart_reset = checkpoint_stats()
    wal_restart2 = wal_stats()
    assert ckpt_restart_reset["count"] < ckpt_restart["count"], (
        "post ckpt reset & restart: checkpoint still reset"
    )
    assert ckpt_restart_reset["reset"] == ckpt_reset["reset"], (
        "post ckpt reset & restart: stats_reset same"
    )
    assert wal_ckpt_reset["records"] < wal_restart2["records"], (
        "post ckpt reset & restart: increased wal record count"
    )
    assert wal_ckpt_reset["bytes"] < wal_restart2["bytes"], (
        "post ckpt reset & restart: increased wal bytes"
    )
    assert wal_start["reset"] == wal_restart2["reset"], (
        "post ckpt reset & restart: wal stats_reset equal"
    )

    # WAL stats stay reset.
    node.sql("SELECT pg_stat_reset_shared('wal')")
    wal_reset = wal_stats()
    assert wal_reset["records"] < wal_restart2["records"], (
        "post wal reset: smaller record count"
    )
    assert wal_reset["bytes"] < wal_restart2["bytes"], "post wal reset: smaller bytes"
    assert wal_reset["reset"] > wal_restart2["reset"], (
        "post wal reset: newer stats_reset"
    )

    node.pg_ctl("restart")
    wal_reset_restart = wal_stats()
    assert wal_reset_restart["records"] < wal_restart2["records"], (
        "post wal reset & restart: smaller record count"
    )
    assert wal_reset["bytes"] < wal_restart2["bytes"], (
        "post wal reset & restart: smaller bytes"
    )
    assert wal_reset["reset"] > wal_restart2["reset"], (
        "post wal reset & restart: newer stats_reset"
    )

    # An immediate restart bumps the WAL stats_reset timestamp.
    node.stop("immediate")
    node.start()
    wal_restart_immediate = wal_stats()
    assert wal_reset_restart["reset"] < wal_restart_immediate["reset"], (
        "post immediate restart: reset timestamp is new"
    )

    node.stop()
