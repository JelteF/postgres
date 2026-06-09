# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of contrib/amcheck/t/005_pitr.pl.

Tests amcheck on an intermediate WAL state reached by point-in-time recovery.
The origin generates a btree with an interrupted page deletion (a VACUUM that
unlinks a leaf page), and a replica is recovered to exactly the LSN of that
VACUUM's UNLINK_PAGE record, where bt_index_parent_check should see (and
tolerate) the interrupted page deletion.
"""

import re
import warnings

SETUP = [
    "BEGIN",
    "CREATE EXTENSION amcheck",
    "CREATE EXTENSION pg_walinspect",
    "CREATE TABLE not_leftmost (c text STORAGE PLAIN)",
    """INSERT INTO not_leftmost
  SELECT repeat(n::text, database_block_size / 4)
  FROM generate_series(1,6) t(n), pg_control_init()""",
    "ALTER TABLE not_leftmost ADD CONSTRAINT not_leftmost_pk PRIMARY KEY (c)",
    "DELETE FROM not_leftmost WHERE c ~ '^[1-4]'",
    "SELECT pg_create_physical_replication_slot('for_walinspect', true, false)",
    "COMMIT",
]


def test_pitr(create_pg):
    origin = create_pg(
        "origin", allows_streaming=True, archiving=True, conf={"autovacuum": False}
    )
    backup = origin.backup("my_backup")

    # Create a btree with 6 PK values spanning 1/4 of a block each, then delete
    # the first four so a leaf page becomes eligible for deletion. The
    # replication slot keeps WAL around for pg_walinspect.
    origin.sql_batch(*SETUP)
    before_vacuum_lsn = origin.lsn("write")

    # VACUUM unlinks the leaf page. Under synchronous_commit=off, force an
    # XLogFlush by dropping a permanent table so pg_walinspect can always see
    # the VACUUM records; then find the LSN of the last UNLINK_PAGE record.
    origin.sql("SET synchronous_commit = off")
    origin.sql("VACUUM (VERBOSE, INDEX_CLEANUP ON) not_leftmost")
    origin.sql("CREATE TABLE XLogFlush ()")
    origin.sql("DROP TABLE XLogFlush")
    unlink_lsn = origin.sql(
        f"SELECT max(start_lsn) FROM pg_get_wal_records_info('{before_vacuum_lsn}', "
        "'FFFFFFFF/FFFFFFFF') WHERE resource_manager = 'Btree' "
        "AND record_type = 'UNLINK_PAGE'"
    )
    assert unlink_lsn, "did not find UNLINK_PAGE record"

    # The recovery target lives in the current WAL segment, so switch to a new
    # segment and wait until the segment holding the target has been archived;
    # otherwise the replica can never replay up to (and promote at) the target.
    walfile = origin.sql(f"SELECT pg_walfile_name('{unlink_lsn}')")
    origin.sql("SELECT pg_switch_wal()")
    origin.poll_query_until(
        "SELECT $1 <= last_archived_wal FROM pg_stat_archiver", walfile
    )

    origin.stop()

    # Recover a replica to exactly (exclusive of) the UNLINK_PAGE LSN, then
    # promote. Its restore_command reads the origin's archived WAL.
    replica = create_pg(
        "replica",
        from_backup=backup,
        restoring=origin,
        conf={
            "recovery_target_lsn": unlink_lsn,
            "recovery_target_inclusive": False,
            "recovery_target_action": "promote",
        },
    )
    replica.poll_query_until("SELECT pg_is_in_recovery() = 'f'")

    # bt_index_parent_check should pass and report the interrupted page deletion
    # (a DEBUG1 message, surfaced by the notice receiver as a warning).
    replica.sql("SET client_min_messages = 'debug1'")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        replica.sql("SELECT bt_index_parent_check('not_leftmost_pk', true)")
    msgs = "\n".join(str(w.message) for w in caught)
    assert re.search("interrupted page deletion detected", msgs), (
        "bt_index_parent_check: interrupted page deletion detected"
    )

    # bt_index_check should also pass.
    replica.sql("SET client_min_messages = 'debug1'")
    replica.sql("SELECT bt_index_check('not_leftmost_pk', true)")
