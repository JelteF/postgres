# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of contrib/vacuumlo/t/001_basic.pl."""


def test_standard_options(pg_bin):
    pg_bin.check_help("vacuumlo")
    pg_bin.check_version("vacuumlo")
    pg_bin.check_bad_option("vacuumlo")
