# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/scripts/t/100_vacuumdb.pl.

Exercises the vacuumdb client program against a running server across its many
option combinations: the VACUUM/ANALYZE variants (-f/-F/-z/-Z, page skipping,
index cleanup, truncate, process main/toast, parallel), --table column lists,
--schema / --exclude-schema, --min-xid-age / --min-mxid-age, --missing-stats-only,
--analyze-only / --analyze-in-stages on partitioned/inherited tables, and the
mutually-exclusive-option error cases. The SQL-in-log checks mirror Perl's
issues_sql_like/issues_sql_unlike.
"""

import pytest

from pypg.bins import vacuumdb


# ---------------------------------------------------------------------------
# --help / --version / bad option
# ---------------------------------------------------------------------------


def test_help_version_options():
    vacuumdb.check_standard_options()


# ---------------------------------------------------------------------------
# Basic VACUUM and its plain option variants. Each runs against the whole
# "postgres" database, so they assert on the per-statement VACUUM/ANALYZE form
# rather than on which relations get processed.
# ---------------------------------------------------------------------------


def test_bare_vacuum(pg):
    with pg.log_contains(r"statement: VACUUM.*;"):
        vacuumdb("postgres", server=pg)


def test_full(pg):
    with pg.log_contains(r"statement: VACUUM \(SKIP_DATABASE_STATS, FULL\).*;"):
        vacuumdb("-f", "postgres", server=pg)


def test_freeze(pg):
    with pg.log_contains(r"statement: VACUUM \(SKIP_DATABASE_STATS, FREEZE\).*;"):
        vacuumdb("-F", "postgres", server=pg)


def test_analyze_with_jobs(pg):
    with pg.log_contains(r"statement: VACUUM \(SKIP_DATABASE_STATS, ANALYZE\).*;"):
        vacuumdb("-zj2", "postgres", server=pg)


def test_analyze_only(pg):
    with pg.log_contains(r"statement: ANALYZE.*;"):
        vacuumdb("-Z", "postgres", server=pg)


def test_disable_page_skipping(pg):
    with pg.log_contains(
        r"statement: VACUUM \(DISABLE_PAGE_SKIPPING, SKIP_DATABASE_STATS\).*;"
    ):
        vacuumdb("--disable-page-skipping", "postgres", server=pg)


def test_skip_locked(pg):
    with pg.log_contains(r"statement: VACUUM \(SKIP_DATABASE_STATS, SKIP_LOCKED\).*;"):
        vacuumdb("--skip-locked", "postgres", server=pg)


def test_skip_locked_analyze_only(pg):
    with pg.log_contains(r"statement: ANALYZE \(SKIP_LOCKED\).*;"):
        vacuumdb("--skip-locked", "--analyze-only", "postgres", server=pg)


def test_no_index_cleanup(pg):
    with pg.log_contains(
        r"statement: VACUUM \(INDEX_CLEANUP FALSE, SKIP_DATABASE_STATS\).*;"
    ):
        vacuumdb("--no-index-cleanup", "postgres", server=pg)


def test_no_truncate(pg):
    with pg.log_contains(
        r"statement: VACUUM \(TRUNCATE FALSE, SKIP_DATABASE_STATS\).*;"
    ):
        vacuumdb("--no-truncate", "postgres", server=pg)


def test_no_process_main(pg):
    with pg.log_contains(
        r"statement: VACUUM \(PROCESS_MAIN FALSE, SKIP_DATABASE_STATS\).*;"
    ):
        vacuumdb("--no-process-main", "postgres", server=pg)


def test_no_process_toast(pg):
    with pg.log_contains(
        r"statement: VACUUM \(PROCESS_TOAST FALSE, SKIP_DATABASE_STATS\).*;"
    ):
        vacuumdb("--no-process-toast", "postgres", server=pg)


def test_parallel_2(pg):
    with pg.log_contains(r"statement: VACUUM \(SKIP_DATABASE_STATS, PARALLEL 2\).*;"):
        vacuumdb("--parallel", "2", "postgres", server=pg)


def test_parallel_0(pg):
    with pg.log_contains(r"statement: VACUUM \(SKIP_DATABASE_STATS, PARALLEL 0\).*;"):
        vacuumdb("--parallel", "0", "postgres", server=pg)


# ---------------------------------------------------------------------------
# Options that are incompatible with --analyze-only, all of which must fail.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "opt",
    [
        "--disable-page-skipping",
        "--no-index-cleanup",
        "--no-truncate",
        "--no-process-main",
        "--no-process-toast",
    ],
)
def test_analyze_only_rejects_vacuum_only_option(pg, opt):
    vacuumdb.check_all("--analyze-only", opt, "postgres", server=pg, exit_code=1)


# ---------------------------------------------------------------------------
# Connection strings and the "-t" table targeting (including the deliberately
# unsafe trailing-command cases).
# ---------------------------------------------------------------------------


def test_connection_string(pg):
    # A conninfo dbname argument should be accepted as the connection target.
    vacuumdb("-Z", "--table=pg_am", "dbname=template1", server=pg)


def test_table_trailing_command_without_columns(pg):
    # "pg_am;ABORT" without a column list is rejected: vacuumdb won't treat the
    # trailing command as part of a relation name.
    vacuumdb.check_all("-Zt", "pg_am;ABORT", "postgres", server=pg, exit_code=1)


def test_table_trailing_command_with_columns(pg):
    # Unwanted, but currently accepted: with a column list, the trailing command
    # sneaks through. The .pl flags this as "better if it failed".
    vacuumdb("-Zt", "pg_am(amname);ABORT", "postgres", server=pg)


# ---------------------------------------------------------------------------
# --table with column lists, including a heavily-quoted identifier.
# ---------------------------------------------------------------------------


def test_table_column_list_quoted(pg):
    pg.sql('CREATE TABLE "need""q(uot" (")x" text)')
    try:
        # The shell-free invocation passes the quoted table+column spec verbatim:
        # quoted identifier "need""q(uot" followed by the quoted column list
        # (")x"). Matches the Perl qw| --table="need""q(uot"(")x") | token.
        vacuumdb("-Z", '--table="need""q(uot"(")x")', "postgres", server=pg)
    finally:
        pg.sql('DROP TABLE "need""q(uot"')


def test_table_analyze_incorrect_column(pg):
    pg.sql("CREATE TABLE vactable (a int, b int)")
    try:
        # Column "c" does not exist, so ANALYZE on that column list fails.
        vacuumdb.check_all(
            "--analyze", "--table", "vactable(c)", "postgres", server=pg, exit_code=1
        )
    finally:
        pg.sql("DROP TABLE vactable")


def test_negative_parallel_degree(pg):
    vacuumdb.check_all("--parallel", "-1", "postgres", server=pg, exit_code=1)


def test_table_analyze_complete_column_list(pg):
    pg.sql("CREATE TABLE vactable (a int, b int)")
    try:
        with pg.log_contains(
            r"statement: VACUUM \(SKIP_DATABASE_STATS, ANALYZE\) public.vactable\(a, b\);"
        ):
            vacuumdb("--analyze", "--table", "vactable(a, b)", "postgres", server=pg)
    finally:
        pg.sql("DROP TABLE vactable")


def test_table_analyze_only_partial_column_list(pg):
    pg.sql("CREATE TABLE vactable (a int, b int)")
    try:
        with pg.log_contains(r"statement: ANALYZE public.vactable\(b\);"):
            vacuumdb("--analyze-only", "--table", "vactable(b)", "postgres", server=pg)
    finally:
        pg.sql("DROP TABLE vactable")


def test_table_view_warns(pg):
    pg.sql("CREATE VIEW vacview AS SELECT 1 as a")
    try:
        # Vacuuming a view emits a WARNING but the run still succeeds.
        vacuumdb.check_all(
            "--analyze",
            "--table",
            "vacview",
            "postgres",
            server=pg,
            exit_code=0,
            stdout=r'^.*vacuuming database "postgres"',
            stderr=r"^WARNING.*cannot vacuum non-tables or special system tables",
        )
    finally:
        pg.sql("DROP VIEW vacview")


# ---------------------------------------------------------------------------
# --min-xid-age / --min-mxid-age.
# ---------------------------------------------------------------------------


def test_min_mxid_age_incorrect_value(pg):
    pg.sql("CREATE TABLE vactable (a int, b int)")
    try:
        vacuumdb.check_all(
            "--table",
            "vactable",
            "--min-mxid-age",
            "0",
            "postgres",
            server=pg,
            exit_code=1,
        )
    finally:
        pg.sql("DROP TABLE vactable")


def test_min_xid_age_incorrect_value(pg):
    pg.sql("CREATE TABLE vactable (a int, b int)")
    try:
        vacuumdb.check_all(
            "--table",
            "vactable",
            "--min-xid-age",
            "0",
            "postgres",
            server=pg,
            exit_code=1,
        )
    finally:
        pg.sql("DROP TABLE vactable")


def test_table_min_mxid_age(pg):
    pg.sql("CREATE TABLE vactable (a int, b int)")
    try:
        with pg.log_contains(r"GREATEST.*relminmxid.*2147483000"):
            vacuumdb(
                "--table",
                "vactable",
                "--min-mxid-age",
                "2147483000",
                "postgres",
                server=pg,
            )
    finally:
        pg.sql("DROP TABLE vactable")


def test_min_xid_age(pg):
    with pg.log_contains(r"GREATEST.*relfrozenxid.*2147483001"):
        vacuumdb("--min-xid-age", "2147483001", "postgres", server=pg)


# ---------------------------------------------------------------------------
# --schema / --exclude-schema and --dry-run.
# ---------------------------------------------------------------------------


@pytest.fixture
def foo_bar_schemas(pg):
    """Create the "Foo" and "Bar" schemas (each with one table) for a single
    test, dropping them afterwards so the schema tests don't leak into siblings.
    """
    pg.sql_batch(
        'CREATE SCHEMA "Foo"',
        'CREATE TABLE "Foo".bar(id int)',
        'CREATE SCHEMA "Bar"',
        'CREATE TABLE "Bar".baz(id int)',
    )
    yield
    pg.sql_batch('DROP SCHEMA "Foo" CASCADE', 'DROP SCHEMA "Bar" CASCADE')


def test_schema(pg, foo_bar_schemas):
    with pg.log_contains(r'VACUUM \(SKIP_DATABASE_STATS\) "Foo".bar'):
        vacuumdb("--schema", '"Foo"', "postgres", server=pg)


def test_dry_run(pg, foo_bar_schemas):
    # --dry-run prints what it would do without issuing the VACUUM.
    with pg.log_contains(r'VACUUM \(SKIP_DATABASE_STATS\) "Foo".bar', times=0):
        vacuumdb("--schema", '"Foo"', "postgres", "--dry-run", server=pg)


def test_multiple_schema_switches(pg, foo_bar_schemas):
    # log_contains uses a bare re.search (no flags), so the (?s) makes "." span
    # the newlines between the two logged statements, like the Perl /s modifier.
    with pg.log_contains(
        r'(?s)VACUUM \(SKIP_DATABASE_STATS\) "Foo".bar'
        r'.*VACUUM \(SKIP_DATABASE_STATS\) "Bar".baz'
    ):
        vacuumdb("--schema", '"Foo"', "--schema", '"Bar"', "postgres", server=pg)


def test_exclude_schema(pg, foo_bar_schemas):
    # (?s) makes "." span newlines so the negative lookahead scans the whole log.
    with pg.log_contains(r'(?s)^(?!.*VACUUM \(SKIP_DATABASE_STATS\) "Foo".bar).*$'):
        vacuumdb("--exclude-schema", '"Foo"', "postgres", server=pg)


def test_multiple_exclude_schema_switches(pg, foo_bar_schemas):
    with pg.log_contains(
        r'(?s)^(?!.*VACUUM \(SKIP_DATABASE_STATS\) "Foo".bar'
        r'| VACUUM \(SKIP_DATABASE_STATS\) "Bar".baz).*$'
    ):
        vacuumdb(
            "--exclude-schema",
            '"Foo"',
            "--exclude-schema",
            '"Bar"',
            "postgres",
            server=pg,
        )


# ---------------------------------------------------------------------------
# Mutually exclusive --schema / --exclude-schema / --table / --all options.
# ---------------------------------------------------------------------------


def test_exclude_schema_and_table_conflict(pg):
    vacuumdb.check_all(
        "--exclude-schema",
        "pg_catalog",
        "--table",
        "pg_class",
        "postgres",
        server=pg,
        exit_code=1,
        stderr=r"cannot vacuum specific table\(s\) and exclude schema\(s\) at the same time",
    )


def test_schema_and_table_conflict(pg):
    vacuumdb.check_all(
        "--schema",
        "pg_catalog",
        "--table",
        "pg_class",
        "postgres",
        server=pg,
        exit_code=1,
        stderr=r"cannot vacuum all tables in schema\(s\) and specific table\(s\) at the same time",
    )


def test_schema_and_exclude_schema_conflict(pg):
    vacuumdb.check_all(
        "--schema",
        "pg_catalog",
        "--exclude-schema",
        '"Foo"',
        "postgres",
        server=pg,
        exit_code=1,
        stderr=r"cannot vacuum all tables in schema\(s\) and exclude schema\(s\) at the same time",
    )


def test_all_exclude_schema(pg):
    with pg.log_contains(
        r"(?:(?!VACUUM \(SKIP_DATABASE_STATS\) pg_catalog.pg_class).)*"
    ):
        vacuumdb("--all", "--exclude-schema", "pg_catalog", server=pg)


def test_all_schema(pg):
    with pg.log_contains(r"VACUUM \(SKIP_DATABASE_STATS\) pg_catalog.pg_class"):
        vacuumdb("--all", "--schema", "pg_catalog", server=pg)


def test_all_table(pg):
    with pg.log_contains(r"VACUUM \(SKIP_DATABASE_STATS\) pg_catalog.pg_class"):
        vacuumdb("--all", "--table", "pg_class", server=pg)


def test_all_and_dbname_option_conflict(pg):
    vacuumdb.check_all(
        "--all",
        "-d",
        "postgres",
        server=pg,
        exit_code=1,
        stderr=r"cannot vacuum all databases and a specific one at the same time",
    )


def test_all_and_dbname_argument_conflict(pg):
    vacuumdb.check_all(
        "--all",
        "postgres",
        server=pg,
        exit_code=1,
        stderr=r"cannot vacuum all databases and a specific one at the same time",
    )


# ---------------------------------------------------------------------------
# --missing-stats-only: only analyze relations that lack statistics. This block
# builds up state incrementally (each new kind of missing stat is added, then
# shown to be filled after one run), so it is kept as a single ordered test that
# creates and drops its own tables on the shared module server.
# ---------------------------------------------------------------------------


def test_missing_stats_only(pg):
    pg.sql_batch(
        "CREATE TABLE regression_vacuumdb_test AS"
        " select generate_series(1, 10) a, generate_series(2, 11) b",
        "ALTER TABLE regression_vacuumdb_test"
        " ADD COLUMN c INT GENERATED ALWAYS AS (a + b)",
    )
    try:
        # --dry-run never issues ANALYZE even when stats are missing.
        with pg.log_contains(r"statement: ANALYZE", times=0):
            vacuumdb(
                "--analyze-only",
                "--dry-run",
                "--missing-stats-only",
                "-t",
                "regression_vacuumdb_test",
                "postgres",
                server=pg,
            )
        # Stats are missing, so the first real run analyzes...
        with pg.log_contains(r"statement: ANALYZE"):
            vacuumdb(
                "--analyze-only",
                "--missing-stats-only",
                "-t",
                "regression_vacuumdb_test",
                "postgres",
                server=pg,
            )
        # ...and a second run finds nothing missing, so it analyzes nothing.
        with pg.log_contains(r"statement: ANALYZE", times=0):
            vacuumdb(
                "--analyze-only",
                "--missing-stats-only",
                "-t",
                "regression_vacuumdb_test",
                "postgres",
                server=pg,
            )

        # A new index expression introduces missing stats again.
        pg.sql(
            "CREATE INDEX regression_vacuumdb_test_idx"
            " ON regression_vacuumdb_test (mod(a, 2))"
        )
        with pg.log_contains(r"statement: ANALYZE"):
            vacuumdb(
                "--analyze-in-stages",
                "--missing-stats-only",
                "-t",
                "regression_vacuumdb_test",
                "postgres",
                server=pg,
            )
        with pg.log_contains(r"statement: ANALYZE", times=0):
            vacuumdb(
                "--analyze-in-stages",
                "--missing-stats-only",
                "-t",
                "regression_vacuumdb_test",
                "postgres",
                server=pg,
            )

        # Extended statistics likewise count as missing until analyzed.
        pg.sql(
            "CREATE STATISTICS regression_vacuumdb_test_stat"
            " ON a, b FROM regression_vacuumdb_test"
        )
        with pg.log_contains(r"statement: ANALYZE"):
            vacuumdb(
                "--analyze-only",
                "--missing-stats-only",
                "-t",
                "regression_vacuumdb_test",
                "postgres",
                server=pg,
            )
        with pg.log_contains(r"statement: ANALYZE", times=0):
            vacuumdb(
                "--analyze-only",
                "--missing-stats-only",
                "-t",
                "regression_vacuumdb_test",
                "postgres",
                server=pg,
            )

        # An inheritance child with its own (analyzed) stats leaves the parent's
        # inherited stats missing until the parent is analyzed.
        pg.sql_batch(
            "CREATE TABLE regression_vacuumdb_child (a INT)"
            " INHERITS (regression_vacuumdb_test)",
            "INSERT INTO regression_vacuumdb_child VALUES (1, 2)",
            "ANALYZE regression_vacuumdb_child",
        )
        with pg.log_contains(r"statement: ANALYZE"):
            vacuumdb(
                "--analyze-in-stages",
                "--missing-stats-only",
                "-t",
                "regression_vacuumdb_test",
                "postgres",
                server=pg,
            )
        with pg.log_contains(r"statement: ANALYZE", times=0):
            vacuumdb(
                "--analyze-in-stages",
                "--missing-stats-only",
                "-t",
                "regression_vacuumdb_test",
                "postgres",
                server=pg,
            )
    finally:
        pg.sql("DROP TABLE regression_vacuumdb_test CASCADE")


def test_missing_stats_only_partitioned(pg):
    # A partition with its own (analyzed) stats leaves the partitioned table's
    # own stats missing until it is analyzed.
    pg.sql_batch(
        "CREATE TABLE regression_vacuumdb_parted (a INT) PARTITION BY LIST (a)",
        "CREATE TABLE regression_vacuumdb_part1 PARTITION OF"
        " regression_vacuumdb_parted FOR VALUES IN (1)",
        "CREATE INDEX ON regression_vacuumdb_parted ((a + 1))",
        "INSERT INTO regression_vacuumdb_parted VALUES (1)",
        "ANALYZE regression_vacuumdb_part1",
    )
    try:
        with pg.log_contains(r"statement: ANALYZE"):
            vacuumdb(
                "--analyze-only",
                "--missing-stats-only",
                "-t",
                "regression_vacuumdb_parted",
                "postgres",
                server=pg,
            )
        with pg.log_contains(r"statement: ANALYZE", times=0):
            vacuumdb(
                "--analyze-only",
                "--missing-stats-only",
                "-t",
                "regression_vacuumdb_parted",
                "postgres",
                server=pg,
            )
    finally:
        pg.sql("DROP TABLE regression_vacuumdb_parted")


# ---------------------------------------------------------------------------
# --analyze-only / --analyze-in-stages cover partitioned tables, and
# --analyze-only never runs VACUUM.
# ---------------------------------------------------------------------------


def test_analyze_only_partitioned_table(pg):
    pg.sql_batch(
        "CREATE TABLE parent_table (a INT) PARTITION BY LIST (a)",
        "CREATE TABLE child_table PARTITION OF parent_table FOR VALUES IN (1)",
        "INSERT INTO parent_table VALUES (1)",
    )
    try:
        # --analyze-only updates statistics for partitioned tables.
        with pg.log_contains(r"statement: ANALYZE public.parent_table"):
            vacuumdb("--analyze-only", "postgres", server=pg)
        # --analyze-in-stages does too.
        with pg.log_contains(r"statement: ANALYZE public.parent_table"):
            vacuumdb("--analyze-in-stages", "postgres", server=pg)
        # --analyze-only does not run vacuum.
        with pg.log_contains(r"statement: VACUUM", times=0):
            vacuumdb("--analyze-only", "postgres", server=pg)
    finally:
        pg.sql("DROP TABLE parent_table")
