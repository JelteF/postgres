# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/pg_walsummary/t/001_basic.pl."""


def test_standard_options(pg_bin):
    pg_bin.check_help("pg_walsummary")
    pg_bin.check_version("pg_walsummary")
    pg_bin.check_bad_option("pg_walsummary")


def test_input_files_required(pg_bin):
    r = pg_bin.run("pg_walsummary")
    assert r.returncode != 0
    assert "no input files specified" in r.stderr
