# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of contrib/pg_visibility/t/002_corrupt_vm.pl.

Checks that pg_check_visible() and pg_check_frozen() report the correct TIDs
when the visibility map disagrees with the heap: tuples are deleted, but an
older copy of the visibility map (still marking those pages all-visible /
all-frozen) is swapped back in, so the deleted tuples must be flagged.
"""

import shutil


def test_corrupt_vm(create_pg):
    # autovacuum off: anything holding a snapshot (including auto-analyze of
    # pg_proc) could stop VACUUM from updating the visibility map.
    node = create_pg("main", conf={"autovacuum": False})

    blck_size = int(node.sql("SHOW block_size"))

    # A table with at least 10 pages (enough to pick 5 random tuples), frozen.
    node.sql_batch(
        "CREATE EXTENSION pg_visibility",
        "CREATE TABLE corruption_test WITH (autovacuum_enabled = false) AS "
        f"SELECT i, repeat('a', 10) AS data FROM generate_series(1, {blck_size}) i",
    )
    # VACUUM can't run inside the implicit transaction of a multi-statement
    # PQexec, so it gets its own call.
    node.sql("VACUUM (FREEZE, DISABLE_PAGE_SKIPPING) corruption_test")

    npages = node.sql("SELECT relpages FROM pg_class WHERE relname = 'corruption_test'")
    assert npages >= 10, "table has at least 10 pages"

    file = node.sql("SELECT pg_relation_filepath('corruption_test')")

    # Delete the first block so it is skipped (neither visible nor frozen).
    node.sql("DELETE FROM corruption_test WHERE (ctid::text::point)[0] = 0")

    # Copy the (current) visibility map aside.
    node.stop()
    vm_file = node.datadir / (file + "_vm")
    vm_temp = node.datadir / (file + "_vm_temp")
    shutil.copy(vm_file, vm_temp)
    node.start()

    # Pick 5 random tuples from the second block onward (block 0 was deleted).
    tuples = node.sql(
        "SELECT ctid FROM ("
        "SELECT ctid FROM corruption_test WHERE (ctid::text::point)[0] != 0 "
        "ORDER BY random() LIMIT 5) ORDER BY ctid ASC"
    )
    in_clause = ", ".join(f"'{t}'" for t in tuples)
    node.sql(f"DELETE FROM corruption_test WHERE ctid in ({in_clause})")

    # Overwrite the visibility map with the old one (still all-visible/frozen).
    node.stop()
    shutil.move(vm_temp, vm_file)
    node.start()

    result = node.sql(
        "SELECT DISTINCT t_ctid FROM pg_check_visible('corruption_test') "
        "ORDER BY t_ctid ASC"
    )
    assert result == tuples, "pg_check_visible must report tuples as corrupted"

    result = node.sql(
        "SELECT DISTINCT t_ctid FROM pg_check_frozen('corruption_test') "
        "ORDER BY t_ctid ASC"
    )
    assert result == tuples, "pg_check_frozen must report tuples as corrupted"
