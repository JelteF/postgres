# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/pg_test_fsync/t/001_basic.pl."""

from pypg.bins import pg_test_fsync


def test_standard_options():
    pg_test_fsync.check_standard_options()


def test_invalid_secs_per_test_argument():
    pg_test_fsync.check_all(
        "--secs-per-test",
        "a",
        exit_code=1,
        stderr="invalid argument for option --secs-per-test",
    )


def test_secs_per_test_out_of_range():
    pg_test_fsync.check_all(
        "--secs-per-test",
        "0",
        exit_code=1,
        stderr="--secs-per-test must be in range 1..4294967295",
    )
