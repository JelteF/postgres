# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/pg_config/t/001_pg_config.pl."""

import re

from pypg.bins import pg_config


def test_standard_options():
    pg_config.check_standard_options()


def test_single_option():
    assert re.search(r"bin", pg_config.capture("--bindir"))


def test_two_options():
    out = pg_config.capture("--bindir", "--libdir")
    assert re.search(r"bin.*\n.*lib", out)


def test_two_options_different_order():
    out = pg_config.capture("--libdir", "--bindir")
    assert re.search(r"lib.*\n.*bin", out)


def test_no_options_prints_many_lines():
    # pg_config with no options prints many lines.
    assert re.search(r".*\n.*\n.*", pg_config.capture())
