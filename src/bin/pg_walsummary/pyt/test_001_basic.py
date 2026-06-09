# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/pg_walsummary/t/001_basic.pl."""

from pypg.bins import pg_walsummary


def test_standard_options():
    pg_walsummary.check_standard_options()


def test_input_files_required():
    pg_walsummary.check_all(exit_code=1, stderr="no input files specified")
