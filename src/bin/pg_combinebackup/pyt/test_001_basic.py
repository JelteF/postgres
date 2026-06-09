# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/pg_combinebackup/t/001_basic.pl."""

from pypg.bins import pg_combinebackup


def test_standard_options():
    pg_combinebackup.check_standard_options()


def test_required_arguments(tmp_path):
    pg_combinebackup.check_all(exit_code=1, stderr=[r"no input directories specified"])
    pg_combinebackup.check_all(
        str(tmp_path), exit_code=1, stderr=[r"no output directory specified"]
    )
