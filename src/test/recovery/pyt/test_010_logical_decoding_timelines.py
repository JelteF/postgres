# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/010_logical_decoding_timelines.pl.

Demonstrate that logical decoding can follow timeline switches.

Logical replication slots can follow timeline switches but it's normally not
possible to have a logical slot on a replica where promotion and a timeline
switch can occur. The only ways to create that circumstance are a
filesystem-level copy of the DB (pg_basebackup excludes pg_replslot but a cold
copy includes it) or by creating the slot directly at the C level. This test
uses the first approach. It also exercises DROP DATABASE on a standby that has
a logical slot in the dropped database.
"""

from libpq import LibpqError
import pytest

# The decoded output expected from the before_basebackup slot after promotion:
# the two pre-promotion inserts plus the post-failover insert.
EXPECTED_BB = [
    "BEGIN",
    "table public.decoding: INSERT: blah[text]:'beforebb'",
    "COMMIT",
    "BEGIN",
    "table public.decoding: INSERT: blah[text]:'afterbb'",
    "COMMIT",
    "BEGIN",
    "table public.decoding: INSERT: blah[text]:'after failover'",
    "COMMIT",
]


def test_logical_decoding_timelines(create_pg):
    primary = create_pg(
        "primary",
        allows_streaming=True,
        archiving=True,
        start=False,
        conf={
            "wal_level": "logical",
            "max_replication_slots": 3,
            "max_wal_senders": 2,
            "log_min_messages": "debug2",
            "hot_standby_feedback": True,
            "wal_receiver_status_interval": 1,
        },
    )
    primary.start()

    # Testing logical timeline following with a filesystem-level copy.
    primary.sql(
        "SELECT pg_create_logical_replication_slot('before_basebackup', 'test_decoding')"
    )
    primary.sql("CREATE TABLE decoding(blah text)")
    primary.sql("INSERT INTO decoding(blah) VALUES ('beforebb')")

    # Verify that DROP DATABASE on a standby with a logical slot works. The
    # only way to get a logical slot on a standby is the same physical-copy
    # trick.
    primary.sql("CREATE DATABASE dropme")
    primary.sql_oneshot(
        "SELECT pg_create_logical_replication_slot('dropme_slot', 'test_decoding')",
        dbname="dropme",
    )

    primary.sql("CHECKPOINT")

    primary.stop()
    backup = primary.backup_fs_cold("b1")
    primary.start()

    primary.sql("SELECT pg_create_physical_replication_slot('phys_slot')")

    replica = create_pg(
        "replica",
        from_backup=backup,
        streaming_primary=primary,
        restoring=primary,
        conf={"primary_slot_name": "phys_slot"},
        start=False,
    )
    replica.start()

    # Dropping 'dropme' on the primary should drop the db and its slot on the
    # standby.
    primary.sql("DROP DATABASE dropme")
    primary.wait_for_catchup(replica)
    assert replica.sql("SELECT 1 FROM pg_database WHERE datname = 'dropme'") == [], (
        "dropped DB dropme on standby"
    )
    assert (
        replica.sql(
            "SELECT plugin FROM pg_replication_slots WHERE slot_name = 'dropme_slot'"
        )
        == []
    ), "logical slot was actually dropped on standby"

    # Back to testing failover.
    primary.sql(
        "SELECT pg_create_logical_replication_slot('after_basebackup', 'test_decoding')"
    )
    primary.sql("INSERT INTO decoding(blah) VALUES ('afterbb')")
    primary.sql("CHECKPOINT")

    # Only the before-basebackup slot should be on the replica.
    assert (
        replica.sql("SELECT slot_name FROM pg_replication_slots ORDER BY slot_name")
        == "before_basebackup"
    ), "Expected to find only slot before_basebackup on replica"

    # hot_standby_feedback must have locked in a catalog_xmin on the physical
    # slot, and any xmin must be >= the catalog_xmin.
    primary.poll_query_until(
        "SELECT catalog_xmin IS NOT NULL FROM pg_replication_slots "
        "WHERE slot_name = 'phys_slot'"
    )
    xmin, catalog_xmin = primary.sql(
        "SELECT xmin::text::bigint, catalog_xmin::text::bigint "
        "FROM pg_replication_slots WHERE slot_name = 'phys_slot'"
    )
    assert xmin is not None, "xmin assigned on physical slot of primary"
    assert catalog_xmin is not None, "catalog_xmin assigned on physical slot of primary"
    # Ignore wrap-around here, we're on a new cluster.
    assert xmin >= catalog_xmin, (
        "xmin on physical slot must not be lower than catalog_xmin"
    )

    primary.sql("CHECKPOINT")
    primary.wait_for_catchup(replica, mode="write")

    # Boom, crash.
    primary.stop("immediate")

    replica.promote()
    replica.sql("INSERT INTO decoding(blah) VALUES ('after failover')")

    # Shouldn't be able to read from the slot created after the base backup.
    with pytest.raises(
        LibpqError, match='replication slot "after_basebackup" does not exist'
    ):
        replica.sql(
            "SELECT data FROM pg_logical_slot_peek_changes('after_basebackup', NULL, "
            "NULL, 'include-xids', '0', 'skip-empty-xacts', '1')"
        )

    # Should be able to read from the slot created before the base backup.
    assert (
        replica.sql(
            "SELECT data FROM pg_logical_slot_peek_changes('before_basebackup', NULL, "
            "NULL, 'include-xids', '0', 'skip-empty-xacts', '1')"
        )
        == EXPECTED_BB
    ), "decoded expected data from slot before_basebackup"

    # We've only peeked so far; fetching the same info over pg_recvlogical
    # should give complete results. Find the commit lsn of the last
    # transaction.
    endpos = replica.sql(
        "SELECT lsn FROM pg_logical_slot_peek_changes('before_basebackup', NULL, NULL) "
        "ORDER BY lsn DESC LIMIT 1"
    )

    # Use the walsender protocol to peek the slot changes and confirm we see the
    # same results.
    stdout = replica.pg_recvlogical_upto(
        "before_basebackup",
        endpos,
        options={"include-xids": "0", "skip-empty-xacts": "1"},
    )
    assert stdout == "\n".join(EXPECTED_BB), (
        "got same output from walsender via pg_recvlogical on before_basebackup"
    )
