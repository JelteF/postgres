# Copyright (c) 2023-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/038_save_logical_slots_shutdown.pl.

Checks that logical replication slots are always flushed to disk during a
shutdown checkpoint: after the publisher restarts, the slot's confirmed_flush
LSN (taken from the walsender's "Streaming transactions committing after ..."
log line) must equal the latest checkpoint location in the control file.
"""

import re

from pypg.bins import pg_controldata

STREAMING_RE = (
    r"Streaming transactions committing after ([A-F0-9]+/[A-F0-9]+), "
    r"reading WAL from ([A-F0-9]+/[A-F0-9]+)\."
)


def _lsn_to_int(lsn):
    hi, lo = lsn.split("/")
    return (int(hi, 16) << 32) | int(lo, 16)


def test_save_logical_slots_shutdown(create_pg):
    publisher = create_pg(
        # Avoid a checkpoint during the test, which would move the latest
        # checkpoint location.
        "pub",
        allows_streaming="logical",
        conf={"checkpoint_timeout": "1h", "autovacuum": False},
    )
    subscriber = create_pg("sub")

    publisher.sql("CREATE TABLE test_tbl (id int)")
    subscriber.sql("CREATE TABLE test_tbl (id int)")

    # Advance the WAL segment so the shutdown checkpoint record from the restart
    # below does not fall onto a new page; otherwise confirmed_flush_lsn and the
    # shutdown checkpoint location won't match.
    publisher.advance_wal(1)

    publisher.sql("INSERT INTO test_tbl VALUES (generate_series(1, 5))")

    publisher.sql("CREATE PUBLICATION pub FOR ALL TABLES")
    subscriber.sql(
        f"CREATE SUBSCRIPTION sub CONNECTION '{publisher.connstr()}' PUBLICATION pub"
    )

    # Wait for the initial table sync to finish.
    subscriber.poll_query_until(
        "SELECT count(1) = 0 FROM pg_subscription_rel WHERE srsubstate NOT IN ('r', 's')"
    )
    assert subscriber.sql("SELECT count(*) FROM test_tbl") == 5, (
        "check initial copy was done"
    )

    offset = publisher.current_log_position()

    # Restart the publisher so the slot is flushed during the shutdown
    # checkpoint. Don't insert more data first (see the advance_wal comment).
    publisher.pg_ctl("restart")

    # The reconnecting walsender logs the slot's confirmed_flush as it starts
    # decoding.
    publisher.wait_for_log(STREAMING_RE, offset)
    match = re.search(STREAMING_RE, publisher.log_since(offset))
    assert match, "could not get confirmed_flush_lsn"
    confirmed_flush = match.group(1)

    # The slot's confirmed_flush LSN must equal the latest checkpoint location.
    control = pg_controldata.capture(publisher.datadir)
    cp_match = re.search(r"^Latest checkpoint location:\s*(\S+)$", control, re.M)
    assert cp_match, "Latest checkpoint location not found in control file"
    assert _lsn_to_int(confirmed_flush) == _lsn_to_int(cp_match.group(1)), (
        "the slot's confirmed_flush LSN is the same as the latest_checkpoint location"
    )
