# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/test_cloexec/t/001_cloexec.pl.

Verifies O_CLOEXEC handling on Windows: handles opened with O_CLOEXEC are not
inherited by child processes, while handles without it are. Windows-specific.
"""

import re
import shutil
import subprocess
import sys

import pytest


def test_cloexec():
    if sys.platform != "win32":
        pytest.skip("test is Windows-specific")

    prog = shutil.which("test_cloexec.exe") or "./test_cloexec.exe"
    r = subprocess.run([prog], capture_output=True, text=True)
    assert r.returncode == 0 and re.search(
        r"SUCCESS.*O_CLOEXEC behavior verified", r.stdout, re.S
    ), f"O_CLOEXEC prevents handle inheritance\nstdout: {r.stdout}\nstderr: {r.stderr}"
