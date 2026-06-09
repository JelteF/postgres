# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/test_json_parser/t/004_test_parser_perf.pl.

Checks that the JSON parser performance tester runs (a single iteration) with
both the recursive-descent and the table-driven parsers. A real performance run
would use thousands of iterations.
"""

from pathlib import Path

from pypg.util import run

TINY_JSON = Path(__file__).resolve().parent.parent / "tiny.json"


def test_parser_perf(tmp_path):
    # Build an array repeating the input JSON 50 times.
    contents = TINY_JSON.read_text()
    fname = tmp_path / "perf.json"
    fname.write_text("[" + ",".join([contents] * 50) + "]")

    # Recursive-descent parser, one iteration.
    r = run("test_json_parser_perf", "1", fname, check=False)
    assert r.returncode == 0, "perf test runs with recursive descent parser"

    # Table-driven (incremental) parser, one iteration.
    r = run("test_json_parser_perf", "-i", "1", fname, check=False)
    assert r.returncode == 0, "perf test runs with table driven parser"
