# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of contrib/auto_explain/t/001_auto_explain.pl."""

import re

import pytest


@pytest.fixture(scope="module", autouse=True)
def auto_explain(pg_server_module):
    """Preload auto_explain (and pg_overexplain) for the whole module and log
    every statement's plan. Applied once at module scope and restored when the
    module finishes."""
    pg_server_module.append_conf(
        **{
            "session_preload_libraries": "pg_overexplain,auto_explain",
            "auto_explain.log_min_duration": 0,
            "auto_explain.log_analyze": True,
        }
    )
    pg_server_module.pg_ctl("reload")


def _options(gucs):
    return " ".join(f"-c {name}={value}" for name, value in gucs.items())


def _set(pg, gucs):
    pg.sql_batch(*(f"SET {name} = '{value}'" for name, value in gucs.items()))


def _reset(pg, gucs):
    pg.sql_batch(*(f"RESET {name}" for name in gucs))


def query_log(pg, sql, gucs=None, user=None):
    """Run ``sql`` and return the server log it produced.

    ``gucs`` are the per-query GUCs the Perl test passed via PGOPTIONS, applied
    with SET on the server's own session; ``user`` runs the query as another
    role instead.
    """
    if user:
        # Connecting as another role is the point of this case, and applying the
        # GUCs at connection time is what makes a rejected one show up in the log
        # as a startup warning rather than a failed statement. Capture the offset
        # before the session is opened so those warnings are in the captured log.
        offset = pg.current_log_position()
        pg.default_connection_options = {"options": _options(gucs or {}), "user": user}
        try:
            pg.sql(sql)
        finally:
            pg.default_connection_options = {}
        return pg.log_since(offset)

    if gucs:
        _set(pg, gucs)
    offset = pg.current_log_position()
    pg.sql(sql)
    log = pg.log_since(offset)
    if gucs:
        _reset(pg, gucs)
    return log


def prepared_query_log(pg, prepare_sql, execute_sql, gucs=None):
    """PREPARE then EXECUTE on the server's session, returning the log the
    EXECUTE produced.

    The two statements are sent separately (as psql would) rather than as a
    single multi-statement string, so auto_explain logs the prepared
    statement's own source text as the Query Text rather than the whole batch.
    """
    if gucs:
        _set(pg, gucs)
    pg.sql(prepare_sql)
    offset = pg.current_log_position()
    pg.sql(execute_sql)
    log = pg.log_since(offset)
    if gucs:
        _reset(pg, gucs)
    return log


def test_simple_query_text_mode(pg):
    log = query_log(pg, "SELECT * FROM pg_class;")
    assert re.search(r"Query Text: SELECT \* FROM pg_class;", log)
    assert not re.search(r"Query Parameters:", log)
    assert re.search(r"Seq Scan on pg_class", log)


def test_prepared_query_text_mode(pg):
    log = prepared_query_log(
        pg,
        "PREPARE get_proc(name) AS SELECT * FROM pg_proc WHERE proname = $1;",
        "EXECUTE get_proc('int4pl');",
    )
    assert re.search(
        r"Query Text: PREPARE get_proc\(name\) AS SELECT \* FROM pg_proc"
        r" WHERE proname = \$1;",
        log,
    )
    assert re.search(r"Query Parameters: \$1 = 'int4pl'", log)
    assert re.search(r"Index Scan using pg_proc_proname_args_nsp_index on pg_proc", log)


def test_prepared_query_truncated_parameters(pg):
    log = prepared_query_log(
        pg,
        "PREPARE get_type(name) AS SELECT * FROM pg_type WHERE typname = $1;",
        "EXECUTE get_type('float8');",
        gucs={"auto_explain.log_parameter_max_length": 3},
    )
    assert re.search(
        r"Query Text: PREPARE get_type\(name\) AS SELECT \* FROM pg_type"
        r" WHERE typname = \$1;",
        log,
    )
    assert re.search(r"Query Parameters: \$1 = 'flo\.\.\.'", log)


def test_prepared_query_parameter_logging_disabled(pg):
    log = prepared_query_log(
        pg,
        "PREPARE get_type(name) AS SELECT * FROM pg_type WHERE typname = $1;",
        "EXECUTE get_type('float8');",
        gucs={"auto_explain.log_parameter_max_length": 0},
    )
    assert re.search(
        r"Query Text: PREPARE get_type\(name\) AS SELECT \* FROM pg_type"
        r" WHERE typname = \$1;",
        log,
    )
    assert not re.search(r"Query Parameters:", log)


def test_query_identifier_logged(pg):
    log = query_log(
        pg,
        "SELECT * FROM pg_class;",
        gucs={"auto_explain.log_verbose": "on", "compute_query_id": "on"},
    )
    assert re.search(r"Query Identifier:", log)


def test_query_identifier_not_logged(pg):
    log = query_log(
        pg,
        "SELECT * FROM pg_class;",
        gucs={"auto_explain.log_verbose": "on", "compute_query_id": "regress"},
    )
    assert not re.search(r"Query Identifier:", log)


def test_json_format(pg):
    log = query_log(
        pg,
        "SELECT * FROM pg_class;",
        gucs={"auto_explain.log_format": "json"},
    )
    assert re.search(r'"Query Text": "SELECT \* FROM pg_class;"', log)
    assert not re.search(r'"Query Parameters":', log)
    assert re.search(
        r'"Node Type": "Seq Scan"[^}]*"Relation Name": "pg_class"', log, re.S
    )


def test_json_format_prepared_query(pg):
    log = prepared_query_log(
        pg,
        "PREPARE get_class(name) AS SELECT * FROM pg_class WHERE relname = $1;",
        "EXECUTE get_class('pg_class');",
        gucs={"auto_explain.log_format": "json"},
    )
    assert re.search(
        r'"Query Text": "PREPARE get_class\(name\) AS SELECT \* FROM pg_class'
        r' WHERE relname = \$1;"',
        log,
    )
    assert re.search(
        r'"Node Type": "Index Scan"[^}]*"Index Name": "pg_class_relname_nsp_index"',
        log,
        re.S,
    )


def test_extension_options(pg):
    log = query_log(
        pg,
        "SELECT 1;",
        gucs={"auto_explain.log_extension_options": "debug"},
    )
    assert re.search(r"Parallel Safe:", log), (
        "extension option produces per-node output"
    )
    assert re.search(r"Command Type: select", log), (
        "extension option produces per-plan output"
    )


def test_suset_parameter_by_non_superuser(pg):
    # PGC_SUSET parameters can be set by a non-superuser only if granted.
    # auto_explain is already preloaded module-wide; here we just need a trust
    # line so we can connect as the test role.
    hba_path = pg.datadir / "pg_hba.conf"
    original_hba = hba_path.read_text()
    hba_path.write_text("local all regress_user1 trust\n" + original_hba)
    pg.pg_ctl("reload")

    pg.sql_batch(
        "CREATE USER regress_user1",
        "GRANT SET ON PARAMETER auto_explain.log_format TO regress_user1",
    )

    log = query_log(
        pg,
        "SELECT * FROM pg_database;",
        gucs={"auto_explain.log_format": "json"},
        user="regress_user1",
    )
    assert re.search(r'"Query Text": "SELECT \* FROM pg_database;"', log)

    log = query_log(
        pg,
        "SELECT * FROM pg_database;",
        gucs={"auto_explain.log_level": "log"},
        user="regress_user1",
    )
    assert re.search(
        r'permission denied to set parameter "auto_explain\.log_level"', log
    )

    pg.sql_batch(
        "REVOKE SET ON PARAMETER auto_explain.log_format FROM regress_user1",
        "DROP USER regress_user1",
    )

    # Restore pg_hba.conf so the trust line doesn't linger for later tests.
    hba_path.write_text(original_hba)
    pg.pg_ctl("reload")


def test_pg_get_loaded_modules(pg):
    # pg_get_loaded_modules() is especially useful for modules with no SQL
    # presence, such as auto_explain.
    row = pg.sql(
        "SELECT module_name,"
        " version = current_setting('server_version') as version_ok,"
        r" regexp_replace(file_name, '\..*', '') as file_name_stripped"
        " FROM pg_get_loaded_modules()"
        " WHERE module_name = 'auto_explain';"
    )
    assert row == ("auto_explain", True, "auto_explain")
