# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/016_min_consistency.pl.

Checks that the minimum recovery LSN in the control file is updated by all
processes that flush pages during recovery, not just the startup process. With
tiny shared_buffers the standby's checkpointer is forced to flush pages and
update minRecoveryPoint; after a clean standby shutdown (and a violent primary
shutdown so no shutdown checkpoint is received), minRecoveryPoint on disk must
not be older than the largest page LSN of the relation.
"""

import pathlib
import re
import struct

from pypg.bins import pg_controldata


def find_largest_lsn(blocksize, filename):
    """Return the largest page LSN ("hi/lo") across all blocks of a relation
    file. The page LSN is the first 8 bytes of each block: two little-endian
    uint32s (xlogid, xrecoff)."""
    max_hi, max_lo = 0, 0
    with open(filename, "rb") as f:
        while block := f.read(blocksize):
            assert len(block) == blocksize, f"short read from {filename}"
            hi, lo = struct.unpack("<II", block[:8])
            if (hi, lo) > (max_hi, max_lo):
                max_hi, max_lo = hi, lo
    return (max_hi, max_lo)


def test_min_consistency(create_pg):
    # Tiny shared_buffers forces the standby to discard/flush buffers so a
    # process other than startup updates minRecoveryPoint; autovacuum off so
    # only the checkpointer flushes pages.
    primary = create_pg(
        "primary",
        allows_streaming=True,
        conf={"shared_buffers": "128kB", "autovacuum": False},
    )
    backup = primary.backup("bkp")
    standby = create_pg("standby", from_backup=backup, streaming_primary=primary)

    primary.sql("CREATE TABLE test1 (a int) WITH (fillfactor = 10)")
    primary.sql("INSERT INTO test1 SELECT generate_series(1, 10000)")

    # A checkpoint plus an update forces post-checkpoint full-page writes, so
    # the startup process replays those pages and advances minRecoveryPoint.
    primary.sql("CHECKPOINT")
    primary.sql("UPDATE test1 SET a = a + 1")
    primary.wait_for_catchup(standby)

    # Fill the standby's shared buffers with that data.
    standby.sql("SELECT count(*) FROM test1")

    # A second update generates no full-page writes, so the startup process
    # replays the records without flushing those pages.
    primary.sql("UPDATE test1 SET a = a + 1")

    blocksize = primary.sql(
        "SELECT setting::int FROM pg_settings WHERE name = 'block_size'"
    )
    relfilenode = primary.sql("SELECT pg_relation_filepath('test1'::regclass)")

    primary.wait_for_catchup(standby)

    # A restart point on the standby makes the checkpointer update
    # minRecoveryPoint.
    standby.sql("CHECKPOINT")

    # Violently stop the primary so the standby gets no shutdown checkpoint,
    # then cleanly stop the standby so its checkpointer writes the restart
    # point's minRecoveryPoint.
    primary.stop("immediate")
    standby.stop("fast")

    # Offline consistency check: minRecoveryPoint must not be older than the
    # largest on-disk page LSN of the relation.
    offline_max_lsn = find_largest_lsn(
        blocksize, pathlib.Path(standby.datadir) / relfilenode
    )

    control = pg_controldata.capture(standby.datadir)
    match = re.search(r"^Minimum recovery ending location:\s*(\S+)$", control, re.M)
    assert match, "No minRecoveryPoint in control file found"
    hi, lo = match.group(1).split("/")
    offline_recovery_lsn = (int(hi, 16), int(lo, 16))

    assert offline_recovery_lsn >= offline_max_lsn, (
        "Check offline that table data is consistent with minRecoveryPoint"
    )
