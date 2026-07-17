# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/pg_test_timing/t/001_basic.pl."""

from pypg.bins import pg_test_timing


def test_standard_options():
    pg_test_timing.check_standard_options()


def test_invalid_duration_argument():
    pg_test_timing.check_all(
        "--duration",
        "a",
        exit_code=1,
        stderr="invalid argument for option --duration",
    )


def test_duration_out_of_range():
    pg_test_timing.check_all(
        "--duration",
        "0",
        exit_code=1,
        stderr="--duration must be in range 1..4294967295",
    )


def test_cutoff_out_of_range():
    pg_test_timing.check_all(
        "--cutoff",
        "101",
        exit_code=1,
        stderr="--cutoff must be in range 0..100",
    )


def test_basic_run_produces_output():
    # We can't check for specific output, but a short run should succeed and
    # produce the expected report sections.
    r = pg_test_timing.check_all(
        "--duration",
        "1",
        exit_code=0,
        stdout=[
            "Testing timing overhead for 1 second.",
            "Histogram of timing durations:",
            "Observed timing durations up to 99.9900%:",
        ],
    )
    assert r.stderr == ""
