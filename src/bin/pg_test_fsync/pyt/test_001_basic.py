# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/pg_test_fsync/t/001_basic.pl."""


def test_standard_options(pg_bin):
    pg_bin.check_help("pg_test_fsync")
    pg_bin.check_version("pg_test_fsync")
    pg_bin.check_bad_option("pg_test_fsync")


def test_invalid_secs_per_test_argument(pg_bin):
    r = pg_bin.run("pg_test_fsync", "--secs-per-test", "a")
    assert r.returncode != 0
    assert "invalid argument for option --secs-per-test" in r.stderr


def test_secs_per_test_out_of_range(pg_bin):
    r = pg_bin.run("pg_test_fsync", "--secs-per-test", "0")
    assert r.returncode != 0
    assert "--secs-per-test must be in range 1..4294967295" in r.stderr
