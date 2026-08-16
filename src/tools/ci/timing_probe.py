"""TEMPORARY: time each TAP test against its pytest port on the same runner.

Runs the two forms of each test alternately, so that whatever else the machine
is doing hits both equally, and prints one TIMING line per run for the caller
to collect from the job log.
"""

import os
import re
import subprocess
import sys

PAIRS = [
    ("test_aio/001_aio", "test_aio/test_001_aio"),
    ("amcheck/001_verify_heapam", "amcheck/test_001_verify_heapam"),
    ("bloom/001_wal", "bloom/test_001_wal"),
    ("recovery/049_wait_for_lsn", "recovery/test_049_wait_for_lsn"),
    ("test_json_parser/002_inline", "test_json_parser/test_002_inline"),
    ("recovery/029_stats_restart", "recovery/test_029_stats_restart"),
    ("recovery/031_recovery_conflict", "recovery/test_031_recovery_conflict"),
    ("subscription/031_column_list", "subscription/test_031_column_list"),
]

PLATFORM = sys.argv[1] if len(sys.argv) > 1 else "unknown"
ROUNDS = int(os.environ.get("ROUNDS", "5"))


def run(test):
    """Run one test on its own and return the duration meson reports."""
    out = subprocess.run(
        ["meson", "test", "-C", "build", "--no-rebuild", "--num-processes", "1", test],
        capture_output=True,
        text=True,
    ).stdout
    m = re.search(r"\b(OK|FAIL|SKIP)\s+([0-9.]+)s", out)
    if not m:
        return "NORESULT"
    return m.group(2) if m.group(1) == "OK" else m.group(1)


# The tests need tmp_install and the initdb cache, which the setup suite
# builds; running a single test by name does not pull them in.
subprocess.run(["meson", "test", "-C", "build", "--suite", "setup"], check=True)

for rnd in range(1, ROUNDS + 1):
    for perl, py in PAIRS:
        for test in (perl, py):
            print(f"TIMING {PLATFORM} {rnd} {test} {run(test)}", flush=True)
