# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/test_int128/t/001_test_int128.pl.

Tests the 128-bit integer arithmetic in int128.h via the test_int128 program.
"""

import subprocess

import pytest

from pypg.util import run


def test_int128():
    # Run the test program with 1M iterations. It is found on PATH because the
    # meson harness adds the suite's build directory there.
    r = run(
        "test_int128",
        1_000_000,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
    )
    if "skipping tests" in r.stdout:
        pytest.skip("no native int128 type")
    assert r.stdout == ""
    assert r.stderr == ""
