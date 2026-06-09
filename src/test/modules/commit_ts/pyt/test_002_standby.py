# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/commit_ts/t/002_standby.pl.

Tests a simple scenario involving a standby: commit timestamps replicated to
a standby match the primary, and turning the feature off on the primary
propagates to the standby.
"""

import pytest

from libpq import LibpqError


def test_standby(create_pg):
    primary = create_pg(
        "committs_primary",
        allows_streaming=True,
        conf={"track_commit_timestamp": True, "max_wal_senders": 5},
    )
    backup = primary.backup("backup")

    standby = create_pg(
        "committs_standby", from_backup=backup, streaming_primary=primary
    )

    for i in range(1, 11):
        primary.sql(f"create table t{i}()")

    ts_query = (
        "SELECT ts.* FROM pg_class, pg_xact_commit_timestamp(xmin) AS ts "
        "WHERE relname = 't10'"
    )
    primary_ts = primary.sql(ts_query)
    primary.wait_for_catchup(standby)
    assert standby.sql(ts_query) == primary_ts, "standby gives same value as primary"

    # Turn the feature off on the primary; the standby replays the parameter
    # change and must then refuse to return commit timestamps.
    primary.append_conf(track_commit_timestamp=False)
    primary.restart()
    primary.sql("checkpoint")
    primary.wait_for_catchup(standby)
    standby.sql("checkpoint")

    with pytest.raises(LibpqError, match="could not get commit timestamp data"):
        standby.sql(ts_query)
