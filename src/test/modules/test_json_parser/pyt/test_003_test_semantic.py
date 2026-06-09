# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/test_json_parser/t/003_test_semantic.pl.

Runs the incremental JSON parser with the semantic-routines option (-s) and
compares its output against the expected tiny.out, across both program flavors
and the -o option.
"""

import subprocess
from pathlib import Path

import pytest

from pypg.util import run

DATA = Path(__file__).resolve().parent.parent
TINY_JSON = DATA / "tiny.json"
TINY_OUT = DATA / "tiny.out"

EXES = [
    ("test_json_parser_incremental",),
    ("test_json_parser_incremental", "-o"),
    ("test_json_parser_incremental_shlib",),
    ("test_json_parser_incremental_shlib", "-o"),
]


@pytest.mark.parametrize("exe", EXES, ids=[" ".join(e) for e in EXES])
def test_semantic(exe):
    r = run(
        *exe,
        "-s",
        TINY_JSON,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
    )
    assert r.stderr == "", "no error output"

    # The program's stdout matches tiny.out. Compare line by line so the
    # comparison ignores line-ending differences (the program's stdout is
    # CRLF-terminated on Windows), mirroring the Perl test's
    # ``diff --strip-trailing-cr``. Read the expected file as UTF-8 to match the
    # subprocess decoding rather than the platform's locale encoding.
    expected = TINY_OUT.read_text(encoding="utf-8")
    assert r.stdout.splitlines() == expected.splitlines(), "no output diff"
