# Copyright (c) 2023-2026, PostgreSQL Global Development Group

"""Port of contrib/pg_stat_statements/t/010_restart.pl.

Check that pg_stat_statements contents are preserved across restarts.
"""

_QUERY = (
    "SELECT query FROM pg_stat_statements"
    " WHERE query NOT LIKE '%pg_stat_statements%' ORDER BY query"
)


def test_stats_preserved_across_restart(create_pg):
    node = create_pg(
        "pgss_restart", conf={"shared_preload_libraries": "pg_stat_statements"}
    )

    with node.connect() as conn:
        conn.sql("CREATE EXTENSION pg_stat_statements")
        conn.sql("CREATE TABLE t1 (a int)")
        conn.sql("SELECT a FROM t1")

        expected = ["CREATE TABLE t1 (a int)", "SELECT a FROM t1"]
        assert conn.sql(_QUERY) == expected, "pg_stat_statements populated"

    node.pg_ctl("restart")
    assert node.sql(_QUERY) == expected, "data kept across restart"

    node.append_conf(**{"pg_stat_statements.save": False})
    node.pg_ctl("reload")
    node.pg_ctl("restart")
    assert (
        node.sql(
            "SELECT count(*) FROM pg_stat_statements"
            " WHERE query NOT LIKE '%pg_stat_statements%'"
        )
        == 0
    ), "data not kept across restart with .save=false"

    node.stop()
