# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of contrib/vacuumlo/t/001_basic.pl."""

from pypg.bins import vacuumlo


def test_standard_options():
    vacuumlo.check_standard_options()
