# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/test_slru/t/002_multixact_wraparound.pl.

Tests multixact wraparound by resetting the cluster's next multixact close to
the wraparound point, then consuming enough multixacts to wrap around.
"""

import re

from pypg.bins import pg_resetwal


def test_multixact_wraparound(create_pg):
    node = create_pg("mxid_wraparound")
    node.append_conf(shared_preload_libraries="test_slru")
    node.stop()  # pg_resetwal and the SLRU fixups need the server offline
    pgdata = node.datadir

    # Set the cluster's next multixact close to wraparound.
    pg_resetwal(
        "--multixact-ids=0xFFFFFFF8,0xFFFFFFF8",
        pgdata,
    )

    # Extract the values needed to fix up the SLRU files.
    out = pg_resetwal.capture("--dry-run", pgdata)

    def grab(label):
        m = re.search(rf"^{label}: *(\d+)$", out, re.M)
        assert m is not None, f"{label!r} not found in pg_resetwal output:\n{out}"
        return int(m.group(1))

    blcksz = grab("Database block size")
    pages_per_segment = grab("Pages per SLRU segment")

    # Initialize the 'offsets' SLRU segment holding the new next multixid with
    # zeros, and remove the old segment.
    offsets_per_page = blcksz // 8  # sizeof(MultiXactOffset) == 8
    segno = 0xFFFFFFF8 // offsets_per_page // pages_per_segment
    bytes_per_seg = pages_per_segment * blcksz
    offsets_dir = pgdata / "pg_multixact" / "offsets"
    (offsets_dir / f"{segno:04X}").write_bytes(b"\0" * bytes_per_seg)
    (offsets_dir / "0000").unlink()

    # Consume multixids to wrap around. Starting at 0xFFFFFFF8, 16 multixacts
    # are more than enough to wrap.
    node.start()
    node.sql("CREATE EXTENSION test_slru")
    multixact_ids = [int(node.sql("SELECT test_create_multixact()")) for _ in range(16)]

    # The last id must be numerically smaller than the first (it wrapped).
    assert multixact_ids[-1] < multixact_ids[0], (
        f"multixact wraparound occurred (first: {multixact_ids[0]}, "
        f"last: {multixact_ids[-1]})"
    )

    # All created multixacts must still be readable.
    for i, multi in enumerate(multixact_ids):
        assert node.sql(f"SELECT test_read_multixact('{multi}')") == "", (
            f"multixact {i} (ID: {multi}) is readable after wraparound"
        )
