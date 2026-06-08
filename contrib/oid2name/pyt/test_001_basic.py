# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of contrib/oid2name/t/001_basic.pl."""


def test_standard_options(pg_bin):
    pg_bin.check_help("oid2name")
    pg_bin.check_version("oid2name")
    pg_bin.check_bad_option("oid2name")
