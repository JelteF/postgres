# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/pg_amcheck/t/001_basic.pl."""

from pypg.bins import pg_amcheck


def test_standard_options():
    pg_amcheck.check_standard_options()
