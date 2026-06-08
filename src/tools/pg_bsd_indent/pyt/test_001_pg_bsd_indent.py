# Copyright (c) 2017-2026, PostgreSQL Global Development Group

"""Port of src/tools/pg_bsd_indent/t/001_pg_bsd_indent.pl.

The test cases come from FreeBSD upstream; this scaffolding is ours. Each
``tests/<name>.0`` input is reindented with its ``tests/<name>.pro`` profile and
the result compared against the expected ``tests/<name>.0.stdout``.
pg_bsd_indent is found on PATH via the suite's build directory.
"""

import difflib
import pathlib
import shutil
import subprocess

import pytest

from pypg.util import run

TESTS_DIR = pathlib.Path(__file__).resolve().parent.parent / "tests"


def test_version():
    r = run(
        "pg_bsd_indent",
        "--version",
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
    )
    assert r.returncode == 0
    assert r.stdout != ""


@pytest.mark.parametrize("src", sorted(TESTS_DIR.glob("*.0")), ids=lambda p: p.stem)
def test_indent(src, tmp_path):
    # Some .pro profiles reference a .list file by bare name, so copy the list
    # files into the working directory and run pg_bsd_indent from there.
    for lst in TESTS_DIR.glob("*.list"):
        shutil.copy(lst, tmp_path)

    out = tmp_path / (src.stem + ".out")
    profile = src.with_suffix(".pro")

    r = run(
        "pg_bsd_indent",
        src,
        out,
        f"-P{profile}",
        cwd=tmp_path,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
    )
    assert r.returncode == 0, r.stderr

    expected = (src.parent / (src.name + ".stdout")).read_text()
    actual = out.read_text()
    assert actual == expected, "".join(
        difflib.unified_diff(
            expected.splitlines(keepends=True),
            actual.splitlines(keepends=True),
            "expected",
            "actual",
        )
    )
