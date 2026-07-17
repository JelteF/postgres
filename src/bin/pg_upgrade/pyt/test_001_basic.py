# Copyright (c) 2022-2026, PostgreSQL Global Development Group

"""Port of src/bin/pg_upgrade/t/001_basic.pl."""

from pypg.bins import pg_upgrade


def test_standard_options():
    pg_upgrade.check_standard_options()
