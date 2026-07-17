# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of contrib/oid2name/t/001_basic.pl."""

from pypg.bins import oid2name


def test_standard_options():
    oid2name.check_standard_options()
