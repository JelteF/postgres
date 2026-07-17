# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/test_misc/t/004_io_direct.pl.

A very simple exercise of the direct I/O GUC (debug_io_direct): generate
shared and local buffer reads and writes, and confirm the data reads back
both normally and after crash recovery.
"""

import os
import sys

import pytest


def _skip_unless_o_direct(tmp_path):
    # macOS (F_NOCACHE) and Windows (FILE_FLAG_NO_BUFFERING) are assumed to
    # support direct I/O on their typical file systems; elsewhere, probe for
    # O_DIRECT support on the file system tmp_path lives on.
    if sys.platform in ("darwin", "win32"):
        return
    if not hasattr(os, "O_DIRECT"):
        pytest.skip("no O_DIRECT")
    try:
        fd = os.open(
            str(tmp_path / "test_o_direct_file"),
            os.O_RDWR | os.O_DIRECT | os.O_CREAT,
        )
    except OSError as e:
        pytest.skip(f"pre-flight test if we can open a file with O_DIRECT failed: {e}")
    os.close(fd)


def test_io_direct(create_pg, tmp_path):
    _skip_unless_o_direct(tmp_path)

    node = create_pg(
        "io_direct",
        conf={
            "debug_io_direct": "data,wal,wal_init",
            "shared_buffers": "256kB",  # tiny to force I/O
            "wal_level": "replica",  # minimal runs out of shared_buffers when so tiny
        },
    )

    # Do some work bound to generate shared and local writes and reads.
    node.sql("create table t1 as select 1 as i from generate_series(1, 10000)")
    node.sql("create table t2count (i int)")
    node.sql_batch(
        "begin",
        "create temporary table t2 as select 1 as i from generate_series(1, 10000)",
        "update t2 set i = i",
        "insert into t2count select count(*) from t2",
        "commit",
    )
    node.sql("update t1 set i = i")
    assert node.sql("select count(*) from t1") == 10000, "read back from shared"
    assert node.sql("select * from t2count") == 10000, "read back from local"

    node.stop("immediate")
    node.start()
    assert node.sql("select count(*) from t1") == 10000, (
        "read back from shared after crash recovery"
    )
    node.stop()
