# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/pg_checksums/t/001_basic.pl."""

from pypg.bins import pg_checksums


def test_standard_options():
    pg_checksums.check_standard_options()
