# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/pg_combinebackup/t/001_basic.pl."""


def test_standard_options(pg_bin):
    pg_bin.check_help("pg_combinebackup")
    pg_bin.check_version("pg_combinebackup")
    pg_bin.check_bad_option("pg_combinebackup")


def test_required_arguments(pg_bin, tmp_path):
    pg_bin.check_all("pg_combinebackup", exit_code=1,
                     stderr=[r"no input directories specified"])
    pg_bin.check_all("pg_combinebackup", str(tmp_path), exit_code=1,
                     stderr=[r"no output directory specified"])
