# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/007_sync_rep.pl.

Minimal test exercising synchronous replication sync_state transitions as
synchronous_standby_names is changed and standbys are started/stopped in
different orders (which rearranges their position in the primary's WalSnd
array).
"""

# Query checking sync_priority and sync_state of each standby. The simplified
# result is a list of (application_name, sync_priority, sync_state) tuples.
CHECK_SQL = (
    "SELECT application_name, sync_priority, sync_state "
    "FROM pg_stat_replication ORDER BY application_name"
)


def test_sync_rep(create_pg):
    primary = create_pg("primary", allows_streaming=True)
    backup = primary.backup("primary_backup")

    def check_sync_state(expected, setting=None):
        # If a setting is given, point synchronous_standby_names at it and
        # reload before polling.
        if setting is not None:
            primary.sql(f"ALTER SYSTEM SET synchronous_standby_names = '{setting}'")
            primary.pg_ctl("reload")
        primary.poll_query_until(CHECK_SQL, expected=expected)

    def start_standby_and_wait(standby):
        # Start a standby and wait until it is registered in the primary's WAL
        # sender array, fixing its ordering relative to the others.
        standby.start()
        primary.poll_query_until(
            "SELECT count(1) = 1 FROM pg_stat_replication "
            f"WHERE application_name = '{standby.name}'"
        )

    # Create all the standbys. Their status on the primary is checked to ensure
    # the ordering of each one of them in the WAL sender array of the primary.
    standby1 = create_pg(
        "standby1", from_backup=backup, streaming_primary=primary, start=False
    )
    standby2 = create_pg(
        "standby2", from_backup=backup, streaming_primary=primary, start=False
    )
    standby3 = create_pg(
        "standby3", from_backup=backup, streaming_primary=primary, start=False
    )

    start_standby_and_wait(standby1)
    start_standby_and_wait(standby2)
    start_standby_and_wait(standby3)

    # sync_state is determined correctly with the old syntax of
    # synchronous_standby_names.
    check_sync_state(
        [
            ("standby1", 1, "sync"),
            ("standby2", 2, "potential"),
            ("standby3", 0, "async"),
        ],
        "standby1,standby2",
    )

    # With "*", all standbys are sync or potential. standby1 is chosen as the
    # sync standby because it is at the head of the WalSnd array even though
    # they share the same priority.
    check_sync_state(
        [
            ("standby1", 1, "sync"),
            ("standby2", 1, "potential"),
            ("standby3", 1, "potential"),
        ],
        "*",
    )

    # Stop and restart standbys to rearrange their order in the WalSnd array.
    # With equal priority, standby2 is now selected first and standby3 next.
    standby1.stop()
    standby2.stop()
    standby3.stop()
    start_standby_and_wait(standby2)
    start_standby_and_wait(standby3)

    # Two sync standbys requested -> two standbys in 'sync' state.
    check_sync_state(
        [("standby2", 2, "sync"), ("standby3", 3, "sync")],
        "2(standby1,standby2,standby3)",
    )

    start_standby_and_wait(standby1)

    create_pg("standby4", from_backup=backup, streaming_primary=primary)

    # standby1 and standby2 appear earlier in synchronous_standby_names -> sync;
    # standby3 appears later -> potential; standby4 not listed -> async.
    check_sync_state(
        [
            ("standby1", 1, "sync"),
            ("standby2", 2, "sync"),
            ("standby3", 3, "potential"),
            ("standby4", 0, "async"),
        ],
    )

    # num_sync exceeds the number of named potential sync standbys.
    check_sync_state(
        [
            ("standby1", 0, "async"),
            ("standby2", 4, "sync"),
            ("standby3", 3, "sync"),
            ("standby4", 1, "sync"),
        ],
        "6(standby4,standby0,standby3,standby2)",
    )

    # "*" before another standby name is acceptable though unusual. standby1 is
    # selected as it has the highest priority, followed by the standby listed
    # first in the WAL sender array (standby2).
    check_sync_state(
        [
            ("standby1", 1, "sync"),
            ("standby2", 2, "sync"),
            ("standby3", 2, "potential"),
            ("standby4", 2, "potential"),
        ],
        "2(standby1,*,standby2)",
    )

    # '2(*)' chooses standby2 and standby3, stored earlier in the WalSnd array.
    check_sync_state(
        [
            ("standby1", 1, "potential"),
            ("standby2", 1, "sync"),
            ("standby3", 1, "sync"),
            ("standby4", 1, "potential"),
        ],
        "2(*)",
    )

    # Stop standby3 (in 'sync' state); the potential standby found earlier in
    # the array (standby1) is promoted to sync.
    standby3.stop()
    check_sync_state(
        [
            ("standby1", 1, "sync"),
            ("standby2", 1, "sync"),
            ("standby4", 1, "potential"),
        ],
    )

    # standby1 and standby2 chosen as sync standbys based on their priorities.
    check_sync_state(
        [("standby1", 1, "sync"), ("standby2", 2, "sync"), ("standby4", 0, "async")],
        "FIRST 2(standby1, standby2)",
    )

    # All listed standbys are candidates in quorum-based sync replication.
    check_sync_state(
        [
            ("standby1", 1, "quorum"),
            ("standby2", 1, "quorum"),
            ("standby4", 0, "async"),
        ],
        "ANY 2(standby1, standby2)",
    )

    # Start standby3, which becomes a quorum candidate.
    standby3.start()
    check_sync_state(
        [
            ("standby1", 1, "quorum"),
            ("standby2", 1, "quorum"),
            ("standby3", 1, "quorum"),
            ("standby4", 1, "quorum"),
        ],
        "ANY 2(*)",
    )
