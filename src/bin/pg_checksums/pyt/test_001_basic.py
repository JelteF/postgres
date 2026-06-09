# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/pg_checksums/t/001_basic.pl."""


def test_standard_options(pg_bin):
    pg_bin.check_help("pg_checksums")
    pg_bin.check_version("pg_checksums")
    pg_bin.check_bad_option("pg_checksums")
