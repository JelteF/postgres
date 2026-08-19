# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/test_json_parser/t/001_test_json_parser_incremental.pl.

Tests the incremental (table-driven) JSON parser via the standalone
test_json_parser_incremental program, in both its statically-linked and
shared-library flavors, with and without the -o (chunk output) option.
"""

import re
import subprocess
from pathlib import Path

import pytest

from pypg.util import run

TINY_JSON = Path(__file__).resolve().parent.parent / "tiny.json"

EXES = [
    ("test_json_parser_incremental",),
    ("test_json_parser_incremental", "-o"),
    ("test_json_parser_incremental_shlib",),
    ("test_json_parser_incremental_shlib", "-o"),
]


@pytest.mark.parametrize("exe", EXES, ids=[" ".join(e) for e in EXES])
def test_json_parser_incremental(exe):
    # Without a file argument the program prints its usage to stderr.
    r = run(
        *exe,
        "-c",
        10,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
    )
    assert re.search("Usage:", r.stderr), "error message if not enough arguments"

    # Small chunk sizes from 64 down to 1 should all succeed.
    for size in range(64, 0, -1):
        r = run(
            *exe,
            "-c",
            size,
            TINY_JSON,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
        )
        assert "SUCCESS" in r.stdout, f"chunk size {size}: test succeeds"
        assert r.stderr == "", f"chunk size {size}: no error output"
