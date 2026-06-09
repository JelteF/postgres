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
        *exe, "-s", TINY_JSON, check=False,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8",
    )
    assert r.stderr == "", "no error output"

    # The program's stdout matches tiny.out verbatim. (The Perl test appends a
    # newline only because its run_command chomps the trailing one from the
    # captured output; subprocess preserves it.)
    assert r.stdout == TINY_OUT.read_text(), "no output diff"
