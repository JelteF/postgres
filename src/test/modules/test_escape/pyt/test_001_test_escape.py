# Copyright (c) 2023-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/test_escape/t/001_test_escape.pl.

Drives the test_escape C program, which exercises libpq's string-escaping
routines against a real server (including a SQL_ASCII database) and emits its
own TAP stream. We surface any failing assertion it reports.
"""

import subprocess

from pypg.util import run


def test_escape(create_pg):
    node = create_pg("node")
    node.sql('CREATE DATABASE db_sql_ascii ENCODING "sql_ascii" TEMPLATE template0;')

    conninfo = f"host={node.host} port={node.port} dbname=db_sql_ascii"
    r = run(
        "test_escape",
        "--conninfo",
        conninfo,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
    )

    assert r.returncode == 0, r.stderr
    assert r.stderr == ""
    failures = [line for line in r.stdout.splitlines() if line.startswith("not ok")]
    assert not failures, "test_escape reported failures:\n" + "\n".join(failures)
