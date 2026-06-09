# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/scripts/t/100_vacuumdb.pl."""

import re


def test_standard_options(pg_bin):
    pg_bin.check_help("vacuumdb")
    pg_bin.check_version("vacuumdb")
    pg_bin.check_bad_option("vacuumdb")


def test_vacuumdb_options(node, pg_bin, sql_like):
    sql_like(node, ["vacuumdb", "postgres"], r"statement: VACUUM.*;")
    sql_like(node, ["vacuumdb", "-f", "postgres"],
             r"statement: VACUUM \(SKIP_DATABASE_STATS, FULL\).*;")
    sql_like(node, ["vacuumdb", "-F", "postgres"],
             r"statement: VACUUM \(SKIP_DATABASE_STATS, FREEZE\).*;")
    sql_like(node, ["vacuumdb", "-zj2", "postgres"],
             r"statement: VACUUM \(SKIP_DATABASE_STATS, ANALYZE\).*;")
    sql_like(node, ["vacuumdb", "-Z", "postgres"], r"statement: ANALYZE.*;")
    sql_like(node, ["vacuumdb", "--disable-page-skipping", "postgres"],
             r"statement: VACUUM \(DISABLE_PAGE_SKIPPING, SKIP_DATABASE_STATS\).*;")
    sql_like(node, ["vacuumdb", "--skip-locked", "postgres"],
             r"statement: VACUUM \(SKIP_DATABASE_STATS, SKIP_LOCKED\).*;")
    sql_like(node, ["vacuumdb", "--skip-locked", "--analyze-only", "postgres"],
             r"statement: ANALYZE \(SKIP_LOCKED\).*;")
    assert pg_bin.run("vacuumdb", "--analyze-only", "--disable-page-skipping", "postgres", server=node).returncode != 0
    sql_like(node, ["vacuumdb", "--no-index-cleanup", "postgres"],
             r"statement: VACUUM \(INDEX_CLEANUP FALSE, SKIP_DATABASE_STATS\).*;")
    assert pg_bin.run("vacuumdb", "--analyze-only", "--no-index-cleanup", "postgres", server=node).returncode != 0
    sql_like(node, ["vacuumdb", "--no-truncate", "postgres"],
             r"statement: VACUUM \(TRUNCATE FALSE, SKIP_DATABASE_STATS\).*;")
    assert pg_bin.run("vacuumdb", "--analyze-only", "--no-truncate", "postgres", server=node).returncode != 0
    sql_like(node, ["vacuumdb", "--no-process-main", "postgres"],
             r"statement: VACUUM \(PROCESS_MAIN FALSE, SKIP_DATABASE_STATS\).*;")
    assert pg_bin.run("vacuumdb", "--analyze-only", "--no-process-main", "postgres", server=node).returncode != 0
    sql_like(node, ["vacuumdb", "--no-process-toast", "postgres"],
             r"statement: VACUUM \(PROCESS_TOAST FALSE, SKIP_DATABASE_STATS\).*;")
    assert pg_bin.run("vacuumdb", "--analyze-only", "--no-process-toast", "postgres", server=node).returncode != 0
    sql_like(node, ["vacuumdb", "--parallel", "2", "postgres"],
             r"statement: VACUUM \(SKIP_DATABASE_STATS, PARALLEL 2\).*;")
    sql_like(node, ["vacuumdb", "--parallel", "0", "postgres"],
             r"statement: VACUUM \(SKIP_DATABASE_STATS, PARALLEL 0\).*;")
    assert pg_bin.run("vacuumdb", "-Z", "--table=pg_am", "dbname=template1", server=node).returncode == 0

    assert pg_bin.run("vacuumdb", "-Zt", "pg_am;ABORT", "postgres", server=node).returncode != 0
    # Unwanted; better if it failed.
    assert pg_bin.run("vacuumdb", "-Zt", "pg_am(amname);ABORT", "postgres", server=node).returncode == 0


def test_vacuumdb_tables_and_schemas(node, pg_bin, sql_like, sql_unlike):
    node.sql(
        'CREATE TABLE "need""q(uot" (")x" text);'
        " CREATE TABLE vactable (a int, b int);"
        " CREATE VIEW vacview AS SELECT 1 as a;"
        " CREATE FUNCTION f0(int) RETURNS int LANGUAGE SQL AS 'SELECT $1 * $1';"
        " CREATE FUNCTION f1(int) RETURNS int LANGUAGE SQL AS 'SELECT f0($1)';"
        " CREATE TABLE funcidx (x int);"
        " INSERT INTO funcidx VALUES (0),(1),(2),(3);"
        ' CREATE SCHEMA "Foo";'
        ' CREATE TABLE "Foo".bar(id int);'
        ' CREATE SCHEMA "Bar";'
        ' CREATE TABLE "Bar".baz(id int);'
    )
    assert pg_bin.run("vacuumdb", "-Z", '--table="need""q(uot"(")x")', "postgres", server=node).returncode == 0

    assert pg_bin.run("vacuumdb", "--analyze", "--table", "vactable(c)", "postgres", server=node).returncode != 0
    assert pg_bin.run("vacuumdb", "--parallel", "-1", "postgres", server=node).returncode != 0
    sql_like(node, ["vacuumdb", "--analyze", "--table", "vactable(a, b)", "postgres"],
             r"statement: VACUUM \(SKIP_DATABASE_STATS, ANALYZE\) public.vactable\(a, b\);")
    sql_like(node, ["vacuumdb", "--analyze-only", "--table", "vactable(b)", "postgres"],
             r"statement: ANALYZE public.vactable\(b\);")
    pg_bin.check_all("vacuumdb", "--analyze", "--table", "vacview", "postgres",
                     exit_code=0, server=node,
                     stdout=[r"^.*vacuuming database \"postgres\""],
                     stderr=[r"^WARNING.*cannot vacuum non-tables or special system tables"])
    assert pg_bin.run("vacuumdb", "--table", "vactable", "--min-mxid-age", "0", "postgres", server=node).returncode != 0
    assert pg_bin.run("vacuumdb", "--table", "vactable", "--min-xid-age", "0", "postgres", server=node).returncode != 0
    sql_like(node, ["vacuumdb", "--table", "vactable", "--min-mxid-age", "2147483000", "postgres"],
             r"GREATEST.*relminmxid.*2147483000")
    sql_like(node, ["vacuumdb", "--min-xid-age", "2147483001", "postgres"],
             r"GREATEST.*relfrozenxid.*2147483001")
    sql_like(node, ["vacuumdb", "--schema", '"Foo"', "postgres"],
             r'VACUUM \(SKIP_DATABASE_STATS\) "Foo".bar')
    sql_unlike(node, ["vacuumdb", "--schema", '"Foo"', "postgres", "--dry-run"],
               r'VACUUM \(SKIP_DATABASE_STATS\) "Foo".bar')
    sql_like(node, ["vacuumdb", "--schema", '"Foo"', "--schema", '"Bar"', "postgres"],
             r'VACUUM \(SKIP_DATABASE_STATS\) "Foo".bar.*VACUUM \(SKIP_DATABASE_STATS\) "Bar".baz')
    sql_like(node, ["vacuumdb", "--exclude-schema", '"Foo"', "postgres"],
             r'^(?!.*VACUUM \(SKIP_DATABASE_STATS\) "Foo".bar).*$')
    sql_like(node, ["vacuumdb", "--exclude-schema", '"Foo"', "--exclude-schema", '"Bar"', "postgres"],
             r'^(?!.*VACUUM \(SKIP_DATABASE_STATS\) "Foo".bar| VACUUM \(SKIP_DATABASE_STATS\) "Bar".baz).*$')

    for args, msg in [
        (["--exclude-schema", "pg_catalog", "--table", "pg_class", "postgres"],
         r"cannot vacuum specific table\(s\) and exclude schema\(s\) at the same time"),
        (["--schema", "pg_catalog", "--table", "pg_class", "postgres"],
         r"cannot vacuum all tables in schema\(s\) and specific table\(s\) at the same time"),
        (["--schema", "pg_catalog", "--exclude-schema", '"Foo"', "postgres"],
         r"cannot vacuum all tables in schema\(s\) and exclude schema\(s\) at the same time"),
    ]:
        r = pg_bin.run("vacuumdb", *args, server=node)
        assert r.returncode != 0
        assert re.search(msg, r.stderr), r.stderr

    sql_like(node, ["vacuumdb", "--all", "--exclude-schema", "pg_catalog"],
             r"(?:(?!VACUUM \(SKIP_DATABASE_STATS\) pg_catalog.pg_class).)*")
    sql_like(node, ["vacuumdb", "--all", "--schema", "pg_catalog"],
             r"VACUUM \(SKIP_DATABASE_STATS\) pg_catalog.pg_class")
    sql_like(node, ["vacuumdb", "--all", "--table", "pg_class"],
             r"VACUUM \(SKIP_DATABASE_STATS\) pg_catalog.pg_class")
    for args, msg in [
        (["--all", "-d", "postgres"], r"cannot vacuum all databases and a specific one at the same time"),
        (["--all", "postgres"], r"cannot vacuum all databases and a specific one at the same time"),
    ]:
        r = pg_bin.run("vacuumdb", *args, server=node)
        assert r.returncode != 0
        assert re.search(msg, r.stderr), r.stderr


def test_vacuumdb_missing_stats_only(node, sql_like, sql_unlike):
    node.sql(
        "CREATE TABLE regression_vacuumdb_test AS"
        " select generate_series(1, 10) a, generate_series(2, 11) b;"
        " ALTER TABLE regression_vacuumdb_test ADD COLUMN c INT GENERATED ALWAYS AS (a + b);"
    )
    sql_unlike(node, ["vacuumdb", "--analyze-only", "--dry-run", "--missing-stats-only",
                      "-t", "regression_vacuumdb_test", "postgres"],
               r"statement: ANALYZE")
    sql_like(node, ["vacuumdb", "--analyze-only", "--missing-stats-only",
                    "-t", "regression_vacuumdb_test", "postgres"],
             r"statement: ANALYZE")
    sql_unlike(node, ["vacuumdb", "--analyze-only", "--missing-stats-only",
                      "-t", "regression_vacuumdb_test", "postgres"],
               r"statement: ANALYZE")

    node.sql("CREATE INDEX regression_vacuumdb_test_idx"
             " ON regression_vacuumdb_test (mod(a, 2));")
    sql_like(node, ["vacuumdb", "--analyze-in-stages", "--missing-stats-only",
                    "-t", "regression_vacuumdb_test", "postgres"],
             r"statement: ANALYZE")
    sql_unlike(node, ["vacuumdb", "--analyze-in-stages", "--missing-stats-only",
                      "-t", "regression_vacuumdb_test", "postgres"],
               r"statement: ANALYZE")

    node.sql("CREATE STATISTICS regression_vacuumdb_test_stat"
             " ON a, b FROM regression_vacuumdb_test;")
    sql_like(node, ["vacuumdb", "--analyze-only", "--missing-stats-only",
                    "-t", "regression_vacuumdb_test", "postgres"],
             r"statement: ANALYZE")
    sql_unlike(node, ["vacuumdb", "--analyze-only", "--missing-stats-only",
                      "-t", "regression_vacuumdb_test", "postgres"],
               r"statement: ANALYZE")

    node.sql("CREATE TABLE regression_vacuumdb_child (a INT)"
             " INHERITS (regression_vacuumdb_test);\n"
             "INSERT INTO regression_vacuumdb_child VALUES (1, 2);\n"
             "ANALYZE regression_vacuumdb_child;\n")
    sql_like(node, ["vacuumdb", "--analyze-in-stages", "--missing-stats-only",
                    "-t", "regression_vacuumdb_test", "postgres"],
             r"statement: ANALYZE")
    sql_unlike(node, ["vacuumdb", "--analyze-in-stages", "--missing-stats-only",
                      "-t", "regression_vacuumdb_test", "postgres"],
               r"statement: ANALYZE")

    node.sql("CREATE TABLE regression_vacuumdb_parted (a INT) PARTITION BY LIST (a);\n"
             "CREATE TABLE regression_vacuumdb_part1 PARTITION OF"
             " regression_vacuumdb_parted FOR VALUES IN (1);\n"
             "INSERT INTO regression_vacuumdb_parted VALUES (1);\n"
             "ANALYZE regression_vacuumdb_part1;\n")
    sql_like(node, ["vacuumdb", "--analyze-only", "--missing-stats-only",
                    "-t", "regression_vacuumdb_parted", "postgres"],
             r"statement: ANALYZE")
    sql_unlike(node, ["vacuumdb", "--analyze-only", "--missing-stats-only",
                      "-t", "regression_vacuumdb_parted", "postgres"],
               r"statement: ANALYZE")


def test_vacuumdb_partitioned_stats(node, sql_like, sql_unlike):
    node.sql("CREATE TABLE parent_table (a INT) PARTITION BY LIST (a);\n"
             "CREATE TABLE child_table PARTITION OF parent_table FOR VALUES IN (1);\n"
             "INSERT INTO parent_table VALUES (1);\n")
    sql_like(node, ["vacuumdb", "--analyze-only", "postgres"],
             r"statement: ANALYZE public.parent_table")
    sql_like(node, ["vacuumdb", "--analyze-in-stages", "postgres"],
             r"statement: ANALYZE public.parent_table")
    sql_unlike(node, ["vacuumdb", "--analyze-only", "postgres"],
               r"statement: VACUUM")
