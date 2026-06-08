# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/pg_config/t/001_pg_config.pl."""

import re


def test_standard_options(pg_bin):
    pg_bin.check_help("pg_config")
    pg_bin.check_version("pg_config")
    pg_bin.check_bad_option("pg_config")


def test_single_option(pg_bin):
    assert re.search(r"bin", pg_bin.run("pg_config", "--bindir").stdout)


def test_two_options(pg_bin):
    out = pg_bin.run("pg_config", "--bindir", "--libdir").stdout
    assert re.search(r"bin.*\n.*lib", out)


def test_two_options_different_order(pg_bin):
    out = pg_bin.run("pg_config", "--libdir", "--bindir").stdout
    assert re.search(r"lib.*\n.*bin", out)


def test_no_options_prints_many_lines(pg_bin):
    # pg_config with no options prints many lines.
    assert re.search(r".*\n.*\n.*", pg_bin.run("pg_config").stdout)
