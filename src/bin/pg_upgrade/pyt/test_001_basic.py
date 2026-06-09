# Copyright (c) 2022-2026, PostgreSQL Global Development Group

"""Port of src/bin/pg_upgrade/t/001_basic.pl."""


def test_standard_options(pg_bin):
    pg_bin.check_help("pg_upgrade")
    pg_bin.check_version("pg_upgrade")
    pg_bin.check_bad_option("pg_upgrade")
