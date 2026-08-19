# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/scripts/t/102_vacuumdb_stages.pl.

Exercises vacuumdb --analyze-in-stages, which runs ANALYZE three times with
progressively relaxed default_statistics_target/vacuum_cost_delay settings.
The SQL-in-log checks mirror Perl's issues_sql_like to verify the per-stage
SET/ANALYZE ordering.
"""

from pypg.bins import vacuumdb


def test_analyze_in_stages_single_database(pg):
    # A single database analyzes three times: stage 1 with target=1, stage 2
    # with target=10, stage 3 resetting the target. (?s) makes . span newlines
    # (Perl's /s); the Perl /x regex's only literal whitespace is the escaped
    # spaces inside each statement, reproduced verbatim here.
    pattern = (
        r"(?s)"
        r"statement: SET default_statistics_target=1; SET vacuum_cost_delay=0;"
        r".*statement: ANALYZE"
        r".*statement: SET default_statistics_target=10; RESET vacuum_cost_delay;"
        r".*statement: ANALYZE"
        r".*statement: RESET default_statistics_target;"
        r".*statement: ANALYZE"
    )
    with pg.log_contains(pattern):
        vacuumdb("--analyze-in-stages", "postgres", server=pg)


def test_analyze_in_stages_all_databases(pg):
    # With --all, each stage runs across every database before advancing to the
    # next stage, so the per-stage SET appears once per database (here template1
    # + postgres) before the target is relaxed for the following stage.
    pattern = (
        r"(?s)"
        r"statement: SET default_statistics_target=1; SET vacuum_cost_delay=0;"
        r".*statement: ANALYZE"
        r".*statement: SET default_statistics_target=1; SET vacuum_cost_delay=0;"
        r".*statement: ANALYZE"
        r".*statement: SET default_statistics_target=10; RESET vacuum_cost_delay;"
        r".*statement: ANALYZE"
        r".*statement: SET default_statistics_target=10; RESET vacuum_cost_delay;"
        r".*statement: ANALYZE"
        r".*statement: RESET default_statistics_target;"
        r".*statement: ANALYZE"
        r".*statement: RESET default_statistics_target;"
        r".*statement: ANALYZE"
    )
    with pg.log_contains(pattern):
        vacuumdb("--analyze-in-stages", "--all", server=pg)
