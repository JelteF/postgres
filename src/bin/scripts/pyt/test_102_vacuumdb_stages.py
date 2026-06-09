# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/scripts/t/102_vacuumdb_stages.pl."""


def test_analyze_in_stages_single_db(node, sql_like):
    sql_like(
        node,
        ["vacuumdb", "--analyze-in-stages", "postgres"],
        r"statement: SET default_statistics_target=1; SET vacuum_cost_delay=0;"
        r".*statement: ANALYZE"
        r".*statement: SET default_statistics_target=10; RESET vacuum_cost_delay;"
        r".*statement: ANALYZE"
        r".*statement: RESET default_statistics_target;"
        r".*statement: ANALYZE",
    )


def test_analyze_in_stages_all_dbs(node, sql_like):
    sql_like(
        node,
        ["vacuumdb", "--analyze-in-stages", "--all"],
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
        r".*statement: ANALYZE",
    )
