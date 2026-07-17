# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/044_invalidate_inactive_slots.pl.

Tests invalidation of idle replication slots. An injection point forces both a
physical and a logical slot to be treated as idle-timed-out during a
checkpoint; each must then be logged as invalidated, report
invalidation_reason = 'idle_timeout', and refuse to be acquired again.
"""

import pytest

import pypg
from libpq import LibpqError


@pypg.require_injection_points()
def test_invalidate_inactive_slots(create_pg):
    node = create_pg(
        "node",
        allows_streaming=True,
        conf={
            "wal_level": "logical",
            "checkpoint_timeout": "1h",
            "idle_replication_slot_timeout": "1min",
        },
    )

    # Logical slot creation cannot run inside a transaction block, so the two
    # slots are created with separate statements.
    node.sql(
        "SELECT pg_create_physical_replication_slot("
        "slot_name := 'physical_slot', immediately_reserve := true)"
    )
    node.sql(
        "SELECT pg_create_logical_replication_slot('logical_slot', 'test_decoding')"
    )

    log_offset = node.current_log_position()

    node.sql("CREATE EXTENSION injection_points")
    # Forcibly cause slot invalidation due to idle_timeout during a checkpoint.
    node.sql("SELECT injection_points_attach('slot-timeout-inval', 'error')")
    node.sql("CHECKPOINT")

    def wait_for_slot_invalidation(slot_name):
        node.wait_for_log(
            f'invalidating obsolete replication slot "{slot_name}"', log_offset
        )
        node.poll_query_until(
            "SELECT COUNT(slot_name) = 1 FROM pg_replication_slots "
            f"WHERE slot_name = '{slot_name}' AND invalidation_reason = 'idle_timeout'"
        )

    wait_for_slot_invalidation("physical_slot")
    wait_for_slot_invalidation("logical_slot")

    # An invalidated slot can no longer be acquired.
    with pytest.raises(
        LibpqError, match='can no longer access replication slot "logical_slot"'
    ):
        node.sql("SELECT pg_replication_slot_advance('logical_slot', '0/1')")
