# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/009_twophase.pl.

Two-phase-commit recovery tests on a pair of nodes (london and paris) that
alternately act as primary and synchronous standby. Exercises committing and
aborting prepared transactions after soft and hard restarts, replaying
duplicate GIDs, shared-memory/lock cleanup during replay, committing prepared
transactions on a promoted standby, MVCC visibility of a prepared transaction
across a standby restart, prepared transactions containing DDL, and the
StartupSUBTRANS path.
"""

from pypg._env import test_timeout_default
from pypg.bins import pgbench

# Final expected contents of t_009_tbl, in id order. The "issued to <node>"
# message records which node was primary when the row's transaction ran.
EXPECTED_TBL = [
    (1, "issued to london"),
    (2, "issued to london"),
    (5, "issued to london"),
    (6, "issued to london"),
    (9, "issued to london"),
    (10, "issued to london"),
    (11, "issued to london"),
    (12, "issued to london"),
    (13, "issued to london"),
    (14, "issued to london"),
    (15, "issued to london"),
    (16, "issued to london"),
    (17, "issued to london"),
    (18, "issued to london"),
    (19, "issued to london"),
    (20, "issued to london"),
    (21, "issued to london"),
    (22, "issued to london"),
    (23, "issued to paris"),
    (24, "issued to paris"),
    (25, "issued to london"),
    (26, "issued to london"),
]


def test_twophase(create_pg, tmp_path):
    london = create_pg(
        "london",
        allows_streaming=True,
        conf={"max_prepared_transactions": 10, "log_checkpoints": True},
    )
    backup = london.backup("london_backup")
    paris = create_pg(
        "paris",
        from_backup=backup,
        streaming_primary=london,
        conf={"subtransaction_buffers": 32},
    )

    def configure_and_reload(node, **gucs):
        node.append_conf(**gucs)
        assert node.sql("SELECT pg_reload_conf()") is True, (
            f"reload node {node.name} with {gucs}"
        )

    # Synchronous replication in both directions, so each node can act as the
    # other's synchronous standby after a role swap.
    configure_and_reload(london, synchronous_standby_names="paris")
    configure_and_reload(paris, synchronous_standby_names="london")

    primary, standby = london, paris
    primary.sql("CREATE TABLE t_009_tbl (id int, msg text)")

    def prepare_two(node, id1, id2, gid):
        """BEGIN, insert id1, savepoint, insert id2, PREPARE TRANSACTION gid."""
        name = node.name
        node.sql_batch(
            "BEGIN",
            f"INSERT INTO t_009_tbl VALUES ({id1}, 'issued to {name}')",
            "SAVEPOINT s1",
            f"INSERT INTO t_009_tbl VALUES ({id2}, 'issued to {name}')",
            f"PREPARE TRANSACTION '{gid}'",
        )

    # Commit/abort after a soft restart: a checkpoint precedes shutdown so no
    # WAL replay happens and 2PC state is rebuilt from the twophase files.
    prepare_two(primary, 1, 2, "xact_009_1")
    prepare_two(primary, 3, 4, "xact_009_2")
    primary.stop()
    primary.start()
    primary.sql("COMMIT PREPARED 'xact_009_1'")
    primary.sql("ROLLBACK PREPARED 'xact_009_2'")

    # Commit/abort after a hard restart: 2PC state is rebuilt from WAL records.
    primary.sql("CHECKPOINT")
    prepare_two(primary, 5, 6, "xact_009_3")
    prepare_two(primary, 7, 8, "xact_009_4")
    primary.stop("immediate")
    primary.start()
    primary.sql("COMMIT PREPARED 'xact_009_3'")
    primary.sql("ROLLBACK PREPARED 'xact_009_4'")

    # WAL replay handles several transactions reusing the same GID.
    primary.sql("CHECKPOINT")
    prepare_two(primary, 9, 10, "xact_009_5")
    primary.sql("COMMIT PREPARED 'xact_009_5'")
    prepare_two(primary, 11, 12, "xact_009_5")
    primary.stop("immediate")
    primary.start()
    primary.sql("COMMIT PREPARED 'xact_009_5'")

    # Replay cleans up shared-memory state and releases locks on commit.
    prepare_two(primary, 13, 14, "xact_009_6")
    primary.sql("COMMIT PREPARED 'xact_009_6'")
    primary.stop("immediate")
    primary.start()
    # This prepare would fail on a GID/lock conflict if replay had not fully
    # cleaned up after the previous commit.
    prepare_two(primary, 15, 16, "xact_009_7")
    primary.sql("COMMIT PREPARED 'xact_009_7'")

    # Replay cleans up shared-memory state on a running standby (no checkpoint).
    prepare_two(primary, 17, 18, "xact_009_8")
    primary.sql("COMMIT PREPARED 'xact_009_8'")
    assert standby.sql("SELECT count(*) FROM pg_prepared_xacts") == 0, (
        "Cleanup of shared memory state on running standby without checkpoint"
    )

    # Same, but force a checkpoint on the standby to use on-disk twophase files.
    prepare_two(primary, 19, 20, "xact_009_9")
    standby.sql("CHECKPOINT")
    primary.sql("COMMIT PREPARED 'xact_009_9'")
    assert standby.sql("SELECT count(*) FROM pg_prepared_xacts") == 0, (
        "Cleanup of shared memory state on running standby after checkpoint"
    )

    # Prepared transactions can be committed on a promoted standby.
    prepare_two(primary, 21, 22, "xact_009_10")
    primary.stop()
    standby.promote()
    primary, standby = standby, primary  # paris is now primary
    # london is down, so this commit cannot wait for synchronous replication.
    with primary.connect() as c:
        c.sql("SET synchronous_commit = off")
        c.sql("COMMIT PREPARED 'xact_009_10'")
    standby.enable_streaming(primary)
    standby.start()

    # Prepared transactions are replayed after a soft restart of the standby
    # while the primary is down (standby uses a distinct startup path).
    prepare_two(primary, 23, 24, "xact_009_11")
    primary.stop()
    standby.pg_ctl("restart")
    standby.promote()
    primary, standby = standby, primary  # london is now primary
    assert primary.sql("SELECT count(*) FROM pg_prepared_xacts") == 1, (
        "Restore prepared transactions from files with primary down"
    )
    standby.enable_streaming(primary)
    standby.start()
    primary.sql("COMMIT PREPARED 'xact_009_11'")

    # Same, but after a hard restart of the standby while the primary is down.
    prepare_two(primary, 25, 26, "xact_009_12")
    primary.stop()
    standby.stop("immediate")
    standby.start()
    standby.promote()
    primary, standby = standby, primary  # paris is now primary
    assert primary.sql("SELECT count(*) FROM pg_prepared_xacts") == 1, (
        "Restore prepared transactions from records with primary down"
    )
    standby.enable_streaming(primary)
    standby.start()
    primary.sql("COMMIT PREPARED 'xact_009_12'")

    # Visibility of a prepared transaction in the standby across a restart.
    name = primary.name
    with primary.connect() as c:
        c.sql("SET synchronous_commit='remote_apply'")  # ensure standby caught up
        c.sql("CREATE TABLE t_009_tbl_standby_mvcc (id int, msg text)")
        c.sql_batch(
            "BEGIN",
            f"INSERT INTO t_009_tbl_standby_mvcc VALUES (1, 'issued to {name}')",
            "SAVEPOINT s1",
            f"INSERT INTO t_009_tbl_standby_mvcc VALUES (2, 'issued to {name}')",
            "PREPARE TRANSACTION 'xact_009_standby_mvcc'",
        )
    primary.stop()
    standby.pg_ctl("restart")

    # Take a repeatable-read snapshot on the standby before the commit.
    standby_session = standby.connect()
    standby_session.sql("BEGIN ISOLATION LEVEL REPEATABLE READ")
    assert standby_session.sql("SELECT count(*) FROM t_009_tbl_standby_mvcc") == 0, (
        "Prepared transaction not visible in standby before commit"
    )

    primary.start()
    with primary.connect() as c:
        c.sql("SET synchronous_commit='remote_apply'")
        c.sql("COMMIT PREPARED 'xact_009_standby_mvcc'")

    # Not visible to the old snapshot, visible to a new one.
    assert standby_session.sql("SELECT count(*) FROM t_009_tbl_standby_mvcc") == 0, (
        "Committed prepared transaction not visible to old snapshot in standby"
    )
    standby_session.sql("COMMIT")
    assert standby_session.sql("SELECT count(*) FROM t_009_tbl_standby_mvcc") == 2, (
        "Committed prepared transaction is visible to new snapshot in standby"
    )
    standby_session.close()

    # Lock conflict between a prepared transaction holding DDL locks and replay
    # of an XLOG_STANDBY_LOCK record issued by a checkpoint.
    name = primary.name
    primary.sql_batch(
        "BEGIN",
        "CREATE TABLE t_009_tbl2 (id int, msg text)",
        "SAVEPOINT s1",
        f"INSERT INTO t_009_tbl2 VALUES (27, 'issued to {name}')",
        "PREPARE TRANSACTION 'xact_009_13'",
    )
    primary.sql("CHECKPOINT")  # issues XLOG_STANDBY_LOCK
    primary.sql("COMMIT PREPARED 'xact_009_13'")

    primary_lsn = primary.lsn("write")
    # The many restarts and promotions above can exhaust the shared per-test
    # deadline, so give this wait its own fresh budget (Perl gives each
    # poll_query_until a separate timeout_default).
    standby.poll_query_until(
        f"SELECT '{primary_lsn}'::pg_lsn <= pg_last_wal_replay_lsn()",
        timeout=test_timeout_default(),
    )
    assert standby.sql("SELECT count(*) FROM t_009_tbl2") == 1, (
        "Replay prepared transaction with DDL"
    )

    def prepare_ddl(node, table, val, gid):
        name = node.name
        node.sql_batch(
            "BEGIN",
            f"CREATE TABLE {table} (id int, msg text)",
            "SAVEPOINT s1",
            f"INSERT INTO {table} VALUES ({val}, 'issued to {name}')",
            f"PREPARE TRANSACTION '{gid}'",
        )

    # Recovery of a prepared transaction with DDL after a hard restart.
    prepare_ddl(primary, "t_009_tbl3", 28, "xact_009_14")
    prepare_ddl(primary, "t_009_tbl4", 29, "xact_009_15")
    primary.stop("immediate")
    primary.start()
    primary.sql("COMMIT PREPARED 'xact_009_14'")
    primary.sql("ROLLBACK PREPARED 'xact_009_15'")

    # Recovery of a prepared transaction with DDL after a soft restart.
    prepare_ddl(primary, "t_009_tbl5", 30, "xact_009_16")
    prepare_ddl(primary, "t_009_tbl6", 31, "xact_009_17")
    primary.stop()
    primary.start()
    primary.sql("COMMIT PREPARED 'xact_009_16'")
    primary.sql("ROLLBACK PREPARED 'xact_009_17'")

    # Expected data on both servers.
    assert primary.sql("SELECT count(*) FROM pg_prepared_xacts") == 0, (
        "No uncommitted prepared transactions on primary"
    )
    assert primary.sql("SELECT * FROM t_009_tbl ORDER BY id") == EXPECTED_TBL, (
        "Check expected t_009_tbl data on primary"
    )
    assert primary.sql("SELECT * FROM t_009_tbl2") == (27, "issued to paris"), (
        "Check expected t_009_tbl2 data on primary"
    )
    assert standby.sql("SELECT count(*) FROM pg_prepared_xacts") == 0, (
        "No uncommitted prepared transactions on standby"
    )
    assert standby.sql("SELECT * FROM t_009_tbl ORDER BY id") == EXPECTED_TBL, (
        "Check expected t_009_tbl data on standby"
    )
    assert standby.sql("SELECT * FROM t_009_tbl2") == (27, "issued to paris"), (
        "Check expected t_009_tbl2 data on standby"
    )

    # Exercise the StartupSUBTRANS 2PC recovery path: leave a prepared
    # transaction open and generate pg_subtrans traffic, then restart.
    standby.stop()
    configure_and_reload(primary, synchronous_standby_names="")
    primary.sql("CHECKPOINT")
    primary.sql("CREATE TABLE test()")
    primary.sql_batch("BEGIN", "CREATE TABLE test1()", "PREPARE TRANSACTION 'foo'")

    subtrans_query = (
        "select 'pg_subtrans/'||f, s.size "
        "from pg_ls_dir('pg_subtrans') f, pg_stat_file('pg_subtrans/'||f) s"
    )
    osubtrans = primary.sql(subtrans_query)

    pgbench_script = tmp_path / "009_twophase.pgb"
    pgbench_script.write_text("insert into test default values\n")
    pgbench(
        "--no-vacuum",
        "--client=5",
        "--transactions=1000",
        "-f",
        pgbench_script,
        server=primary,
    )

    primary.stop()
    primary.start()
    nsubtrans = primary.sql(subtrans_query)
    assert osubtrans != nsubtrans, "contents of pg_subtrans/ have changed"
