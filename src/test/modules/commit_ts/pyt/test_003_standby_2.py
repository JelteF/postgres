# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/commit_ts/t/003_standby_2.pl.

A primary/standby scenario where track_commit_timestamp is toggled on and off
repeatedly, then the standby is promoted.
"""

import pytest

from libpq import LibpqError

TS_QUERY = (
    "SELECT ts.* FROM pg_class, pg_xact_commit_timestamp(xmin) AS ts "
    "WHERE relname = '{}'"
)


def test_standby_2(create_pg):
    primary = create_pg(
        "committs2_primary",
        allows_streaming=True,
        conf={"track_commit_timestamp": True, "max_wal_senders": 5},
    )
    backup = primary.backup("backup")

    standby = create_pg(
        "committs2_standby", from_backup=backup, streaming_primary=primary
    )

    for i in range(1, 11):
        primary.sql(f"create table t{i}()")

    # Turn the feature off on the primary and let the standby replay it.
    primary.append_conf(track_commit_timestamp=False)
    primary.restart()
    primary.sql("checkpoint")
    primary.wait_for_catchup(standby)
    standby.sql("checkpoint")
    standby.restart()

    # The standby has replayed the feature being off, so it can no longer
    # return commit timestamps.
    with pytest.raises(LibpqError, match="could not get commit timestamp data"):
        standby.sql(TS_QUERY.format("t10"))

    # Toggle the feature on the primary again.
    primary.append_conf(track_commit_timestamp=True)
    primary.restart()
    primary.append_conf(track_commit_timestamp=False)
    primary.restart()

    # After promotion the (now-primary) node uses its own configuration, which
    # still has track_commit_timestamp on, so new commits get timestamps.
    standby.promote()
    standby.sql("create table t11()")
    ts = standby.sql(TS_QUERY.format("t11"))
    assert ts is not None, "standby gives a valid value after promotion"
