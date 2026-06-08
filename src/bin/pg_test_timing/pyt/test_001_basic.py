# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/pg_test_timing/t/001_basic.pl."""


def test_standard_options(pg_bin):
    pg_bin.check_help("pg_test_timing")
    pg_bin.check_version("pg_test_timing")
    pg_bin.check_bad_option("pg_test_timing")


def test_invalid_duration_argument(pg_bin):
    r = pg_bin.run("pg_test_timing", "--duration", "a")
    assert r.returncode != 0
    assert "invalid argument for option --duration" in r.stderr


def test_duration_out_of_range(pg_bin):
    r = pg_bin.run("pg_test_timing", "--duration", "0")
    assert r.returncode != 0
    assert "--duration must be in range 1..4294967295" in r.stderr


def test_cutoff_out_of_range(pg_bin):
    r = pg_bin.run("pg_test_timing", "--cutoff", "101")
    assert r.returncode != 0
    assert "--cutoff must be in range 0..100" in r.stderr


def test_basic_run_produces_output(pg_bin):
    # We can't check for specific output, but a short run should succeed and
    # produce the expected report sections.
    r = pg_bin.run("pg_test_timing", "--duration", "1")
    assert r.returncode == 0
    assert r.stderr == ""
    assert "Testing timing overhead for 1 second." in r.stdout
    assert "Histogram of timing durations:" in r.stdout
    assert "Observed timing durations up to 99.9900%:" in r.stdout
