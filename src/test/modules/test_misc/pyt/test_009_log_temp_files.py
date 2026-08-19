# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/test_misc/t/009_log_temp_files.pl.

Checks how temporary file removals and statement queries are associated in
the server logs for various query sequences with the simple and extended
query protocols.

The extended-protocol cases drive libpq directly rather than psql: exec_params
is psql's ``\\bind ... \\g``, prepare()/PreparedStatement.exec() are
``\\parse`` / ``\\bind_named``, and the pipeline() context manager is
``\\startpipeline`` ... ``\\endpipeline``.
"""

import re

import pytest


def test_log_temp_files(create_pg):
    # The extended-protocol cases need PGconn.exec_params() and the pipeline()
    # context manager (send_query/send_query_params), which were dropped in the
    # framework rewrite and not yet reintroduced. Skip until they are.
    pytest.skip("PGconn extended-protocol/pipeline helpers not yet reintroduced")

    node = create_pg(
        "log_temp_files",
        conf={
            "work_mem": "64kB",
            "log_temp_files": 0,
            "debug_parallel_query": False,
            "log_error_verbosity": "default",
        },
    )

    # Setup table and populate with data.
    node.sql("CREATE UNLOGGED TABLE foo(a int)")
    node.sql("INSERT INTO foo(a) SELECT * FROM generate_series(1, 5000)")

    # unnamed portal: temporary file dropped under second SELECT query. The
    # unnamed portal (and its sort's temp file) survives the extended-protocol
    # Sync, so it is only dropped when the next command runs.
    offset = node.current_log_position()
    with node.connect() as c:
        c.sql("BEGIN")
        c.exec_params("SELECT a FROM foo ORDER BY a OFFSET $1", 4990)
        c.sql("SELECT 'unnamed portal'")
        c.sql("END")
    assert re.search(
        r"LOG:\s+temporary file: path.*\n.* STATEMENT:\s+SELECT 'unnamed portal'",
        node.log_since(offset),
        re.DOTALL,
    ), "unnamed portal"

    # bind and implicit transaction: temporary file dropped without query. The
    # implicit transaction commits at the Sync, dropping the temp file with no
    # following statement to attribute it to.
    offset = node.current_log_position()
    with node.connect() as c:
        c.exec_params("SELECT a FROM foo ORDER BY a OFFSET $1", 4991)
    log = node.log_since(offset)
    assert re.search(r"LOG:\s+temporary file:", log), "temporary file removed"
    assert not re.search(r"STATEMENT:", log), "no statement logged"

    # named portal: temporary file dropped under second SELECT query.
    offset = node.current_log_position()
    node.sql("BEGIN")
    with node.prepare("SELECT a FROM foo ORDER BY a OFFSET $1") as stmt:
        stmt.exec(4999)
        node.sql("SELECT 'named portal'")
    node.sql("END")
    assert re.search(
        r"LOG:\s+temporary file: path.*\n.* STATEMENT:\s+SELECT 'named portal'",
        node.log_since(offset),
        re.DOTALL,
    ), "named portal"

    # pipelined query: temporary file dropped under second SELECT query.
    offset = node.current_log_position()
    with node.connect() as c:
        with c.pipeline() as p:
            p.send_query_params("SELECT a FROM foo ORDER BY a OFFSET $1", 4992)
            p.send_query("SELECT 'pipelined query'")
    assert re.search(
        r"LOG:\s+temporary file: path.*\n.* STATEMENT:\s+SELECT 'pipelined query'",
        node.log_since(offset),
        re.DOTALL,
    ), "pipelined query"

    # parse and bind: temporary file dropped without query.
    offset = node.current_log_position()
    # The temp file is dropped at the Sync ending exec()'s implicit
    # transaction, so this works on the default connection: no connection
    # close is needed to trigger the drop.
    with node.prepare("SELECT a, a, a FROM foo ORDER BY a OFFSET $1") as p1:
        p1.exec(4993)
    log = node.log_since(offset)
    assert re.search(r"LOG:\s+temporary file:", log), "temporary file removed"
    assert not re.search(r"STATEMENT:", log), "no statement logged"

    # simple query: temporary file dropped under the SELECT query itself.
    offset = node.current_log_position()
    with node.connect() as c:
        c.sql("BEGIN;")
        c.sql("SELECT a FROM foo ORDER BY a OFFSET 4994;")
        c.sql("END;")
    assert re.search(
        r"LOG:\s+temporary file: path.*\n.* STATEMENT:\s+"
        r"SELECT a FROM foo ORDER BY a OFFSET 4994;",
        node.log_since(offset),
        re.DOTALL,
    ), "simple query"

    # cursor: temporary file dropped under CLOSE.
    offset = node.current_log_position()
    with node.connect() as c:
        c.sql("BEGIN;")
        c.sql("DECLARE mycur CURSOR FOR SELECT a FROM foo ORDER BY a OFFSET 4995;")
        c.sql("FETCH 10 FROM mycur;")
        c.sql("SELECT 1;")
        c.sql("CLOSE mycur;")
        c.sql("END;")
    assert re.search(
        r"LOG:\s+temporary file: path.*\n.* STATEMENT:\s+CLOSE mycur;",
        node.log_since(offset),
        re.DOTALL,
    ), "cursor"

    # cursor WITH HOLD: temporary file dropped under COMMIT.
    offset = node.current_log_position()
    with node.connect() as c:
        c.sql("BEGIN;")
        c.sql(
            "DECLARE holdcur CURSOR WITH HOLD FOR "
            "SELECT a FROM foo ORDER BY a OFFSET 4996;"
        )
        c.sql("FETCH 10 FROM holdcur;")
        c.sql("COMMIT;")
        c.sql("CLOSE holdcur;")
    assert re.search(
        r"LOG:\s+temporary file: path.*\n.* STATEMENT:\s+COMMIT;",
        node.log_since(offset),
        re.DOTALL,
    ), "cursor WITH HOLD"

    # prepare/execute: temporary file dropped under EXECUTE.
    offset = node.current_log_position()
    with node.connect() as c:
        c.sql("BEGIN;")
        c.sql("PREPARE p1 AS SELECT a FROM foo ORDER BY a OFFSET 4997;")
        c.sql("EXECUTE p1;")
        c.sql("DEALLOCATE p1;")
        c.sql("END;")
    assert re.search(
        r"LOG:\s+temporary file: path.*\n.* STATEMENT:\s+EXECUTE p1;",
        node.log_since(offset),
        re.DOTALL,
    ), "prepare/execute"

    node.stop("fast")
