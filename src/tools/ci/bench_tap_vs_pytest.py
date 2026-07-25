#!/usr/bin/env python3
"""Benchmark the pytest ports against the TAP tests they replace.

Comparing timings across two CI runs is not trustworthy: we measured the same
test at 39.4s and 61.6s in two runs of identical code, because runners differ
and the rest of the suite competes for the machine. This runs both forms of
each test on one runner, one at a time, several times over, so the comparison
is between numbers gathered under the same conditions.

Three things make the measurement fair:

- Only one test runs at a time (--num-processes 1, one test per invocation), so
  neither form is slowed down by the rest of the suite running alongside it.
- The two forms of a test run back to back, so a machine that is slow for a
  while affects both roughly equally.
- Which form goes first alternates from repetition to repetition, so neither
  systematically benefits from the other having just warmed the page cache.

Durations come from meson's own JSON log rather than from wall-clock time
around the subprocess, so meson's startup cost is excluded, and from the log
rather than from stdout because the human-readable line format differs between
meson versions.
"""

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys

# (meson suite, TAP test name, pytest test name)
PAIRS = [
    ("test_aio", "001_aio", "test_001_aio"),
    ("amcheck", "001_verify_heapam", "test_001_verify_heapam"),
    ("bloom", "001_wal", "test_001_wal"),
    ("recovery", "049_wait_for_lsn", "test_049_wait_for_lsn"),
    ("test_json_parser", "002_inline", "test_002_inline"),
    ("recovery", "029_stats_restart", "test_029_stats_restart"),
    ("recovery", "031_recovery_conflict", "test_031_recovery_conflict"),
    ("subscription", "031_column_list", "test_031_column_list"),
]

LOGBASE = "bench"


def run_one(build, suite, test):
    """Run a single test on its own and return (result, duration).

    The name check is exact enough to tell the two forms apart: the pytest
    entry for 001_wal is .../test_001_wal, which does not end in "/001_wal".
    """
    subprocess.run(
        [
            "meson", "test", "-C", build, "--no-rebuild",
            "--num-processes", "1", "--logbase", LOGBASE,
            "{}/{}".format(suite, test),
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    path = os.path.join(build, "meson-logs", LOGBASE + ".json")
    entry = None
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                if d.get("name", "").endswith("/" + test):
                    entry = d
    except FileNotFoundError:
        return ("NOLOG", float("nan"))

    if entry is None:
        return ("NOTFOUND", float("nan"))
    return (entry.get("result", "?"), float(entry.get("duration", float("nan"))))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default="build")
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--csv", default="bench_tap_vs_pytest.csv")
    args = ap.parse_args()

    print("platform: {} {} / python {}".format(
        platform.system(), platform.machine(), platform.python_version()))
    print("reps: {}  pairs: {}\n".format(args.reps, len(PAIRS)), flush=True)

    rows = []
    for rep in range(1, args.reps + 1):
        for suite, tap, pyt in PAIRS:
            forms = [("pytest", pyt), ("tap", tap)]
            if rep % 2 == 0:
                forms.reverse()
            for form, test in forms:
                result, dur = run_one(args.build, suite, test)
                rows.append({
                    "rep": rep, "suite": suite, "form": form,
                    "test": test, "result": result, "duration": dur,
                })
                print("rep {:<2} {:<6} {:<45} {:<9} {:8.2f}s".format(
                    rep, form, suite + "/" + test, result, dur), flush=True)
        print("", flush=True)

    with open(args.csv, "w") as fh:
        fh.write("rep,suite,form,test,result,duration\n")
        for r in rows:
            fh.write("{rep},{suite},{form},{test},{result},{duration:.3f}\n".format(**r))
    print("wrote {}\n".format(args.csv))

    def samples(suite, test, form):
        return [r["duration"] for r in rows
                if r["suite"] == suite and r["form"] == form
                and r["test"] == test and r["result"] == "OK"]

    print("=" * 100)
    print("{:<38} {:>17} {:>17} {:>15}".format(
        "test", "TAP min/median", "pytest min/median", "speedup (med)"))
    print("=" * 100)
    tot_tap = tot_pyt = 0.0
    for suite, tap, pyt in PAIRS:
        t = samples(suite, tap, "tap")
        p = samples(suite, pyt, "pytest")
        if not t or not p:
            print("{:<38} {:>17} {:>17} {:>15}".format(
                suite + "/" + tap, "n=%d" % len(t), "n=%d" % len(p), "incomplete"))
            continue
        tm, pm = statistics.median(t), statistics.median(p)
        tot_tap += tm
        tot_pyt += pm
        print("{:<38} {:>17} {:>17} {:>14.1f}x".format(
            suite + "/" + tap,
            "%.1f / %.1f" % (min(t), tm),
            "%.1f / %.1f" % (min(p), pm),
            tm / pm if pm else float("nan")))
    print("-" * 100)
    if tot_pyt:
        print("{:<38} {:>17} {:>17} {:>14.1f}x".format(
            "TOTAL (sum of medians)",
            "%.1f" % tot_tap, "%.1f" % tot_pyt, tot_tap / tot_pyt))
        print("\nTAP {:.0f}s -> pytest {:.0f}s: {:.0f}% less time".format(
            tot_tap, tot_pyt, 100 * (tot_tap - tot_pyt) / tot_tap))

    bad = [r for r in rows if r["result"] != "OK"]
    if bad:
        print("\n{} of {} runs did not pass:".format(len(bad), len(rows)))
        for r in bad:
            print("  rep {rep} {form} {suite}/{test}: {result}".format(**r))
        return 1
    return 0


sys.exit(main())
