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


def query_log(pg, conn, sql, gucs=None, user=None):
    """Run ``sql`` on ``conn`` and return the server log it produced.

    ``gucs`` (per-query GUCs, the equivalent of the Perl test's PGOPTIONS) and
    ``user`` require a dedicated connection, so one is opened in that case;
    otherwise the supplied ``conn`` is reused.
    """
    # Capture the offset before opening any new connection, so that warnings
    # emitted while applying per-connection options (e.g. a permission-denied
    # GUC) are part of the captured log.
    offset = pg.current_log_position()
    if gucs or user:
        kwargs = {}
        if gucs:
            kwargs["options"] = _options(gucs)
        if user:
            kwargs["user"] = user
        conn = pg.connect(**kwargs)

    conn.sql(sql)
    return pg.log_since(offset)


def prepared_query_log(pg, conn, prepare_sql, execute_sql, gucs=None):
    """PREPARE then EXECUTE on one connection, returning the log the EXECUTE
    produced.

    The two statements are sent separately (as psql would) rather than as a
    single multi-statement string, so auto_explain logs the prepared
    statement's own source text as the Query Text rather than the whole batch.
    """
    if gucs:
        conn = pg.connect(options=_options(gucs))

    conn.sql(prepare_sql)
    offset = pg.current_log_position()
    conn.sql(execute_sql)
    return pg.log_since(offset)


def test_simple_query_text_mode(pg, conn):
    log = query_log(pg, conn, "SELECT * FROM pg_class;")
    assert re.search(r"Query Text: SELECT \* FROM pg_class;", log)
    assert not re.search(r"Query Parameters:", log)
    assert re.search(r"Seq Scan on pg_class", log)


def test_prepared_query_text_mode(pg, conn):
    log = prepared_query_log(
        pg,
        conn,
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


def test_prepared_query_truncated_parameters(pg, conn):
    log = prepared_query_log(
        pg,
        conn,
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


def test_prepared_query_parameter_logging_disabled(pg, conn):
    log = prepared_query_log(
        pg,
        conn,
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


def test_query_identifier_logged(pg, conn):
    log = query_log(
        pg,
        conn,
        "SELECT * FROM pg_class;",
        gucs={"auto_explain.log_verbose": "on", "compute_query_id": "on"},
    )
    assert re.search(r"Query Identifier:", log)


def test_query_identifier_not_logged(pg, conn):
    log = query_log(
        pg,
        conn,
        "SELECT * FROM pg_class;",
        gucs={"auto_explain.log_verbose": "on", "compute_query_id": "regress"},
    )
    assert not re.search(r"Query Identifier:", log)


def test_json_format(pg, conn):
    log = query_log(
        pg,
        conn,
        "SELECT * FROM pg_class;",
        gucs={"auto_explain.log_format": "json"},
    )
    assert re.search(r'"Query Text": "SELECT \* FROM pg_class;"', log)
    assert not re.search(r'"Query Parameters":', log)
    assert re.search(
        r'"Node Type": "Seq Scan"[^}]*"Relation Name": "pg_class"', log, re.S
    )


def test_json_format_prepared_query(pg, conn):
    log = prepared_query_log(
        pg,
        conn,
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


def test_extension_options(pg, conn):
    log = query_log(
        pg,
        conn,
        "SELECT 1;",
        gucs={"auto_explain.log_extension_options": "debug"},
    )
    assert re.search(r"Parallel Safe:", log), (
        "extension option produces per-node output"
    )
    assert re.search(r"Command Type: select", log), (
        "extension option produces per-plan output"
    )


def test_suset_parameter_by_non_superuser(pg, conn):
    # PGC_SUSET parameters can be set by a non-superuser only if granted.
    # auto_explain is already preloaded module-wide; here we just need a trust
    # line so we can connect as the test role.
    hba_path = pg.datadir / "pg_hba.conf"
    original_hba = hba_path.read_text()
    hba_path.write_text("local all regress_user1 trust\n" + original_hba)
    pg.pg_ctl("reload")

    conn.sql_batch(
        "CREATE USER regress_user1",
        "GRANT SET ON PARAMETER auto_explain.log_format TO regress_user1",
    )

    log = query_log(
        pg,
        conn,
        "SELECT * FROM pg_database;",
        gucs={"auto_explain.log_format": "json"},
        user="regress_user1",
    )
    assert re.search(r'"Query Text": "SELECT \* FROM pg_database;"', log)

    log = query_log(
        pg,
        conn,
        "SELECT * FROM pg_database;",
        gucs={"auto_explain.log_level": "log"},
        user="regress_user1",
    )
    assert re.search(
        r'permission denied to set parameter "auto_explain\.log_level"', log
    )

    conn.sql_batch(
        "REVOKE SET ON PARAMETER auto_explain.log_format FROM regress_user1",
        "DROP USER regress_user1",
    )

    # Restore pg_hba.conf so the trust line doesn't linger for later tests.
    hba_path.write_text(original_hba)
    pg.pg_ctl("reload")


def test_pg_get_loaded_modules(conn):
    # pg_get_loaded_modules() is especially useful for modules with no SQL
    # presence, such as auto_explain.
    row = conn.sql(
        "SELECT module_name,"
        " version = current_setting('server_version') as version_ok,"
        r" regexp_replace(file_name, '\..*', '') as file_name_stripped"
        " FROM pg_get_loaded_modules()"
        " WHERE module_name = 'auto_explain';"
    )
    assert row == ("auto_explain", True, "auto_explain")
