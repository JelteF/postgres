# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/scripts/t/090_reindexdb.pl."""

# Save a set of relfilenodes from the catalogs so they can be cross-checked
# against pg_class after a REINDEX. Mirrors the queries in the Perl test.
FETCH_TOAST_RELFILENODES = """SELECT b.oid::regclass, c.oid::regclass::text, c.oid, c.relfilenode
  FROM pg_class a
    JOIN pg_class b ON (a.oid = b.reltoastrelid)
    JOIN pg_index i on (a.oid = i.indrelid)
    JOIN pg_class c on (i.indexrelid = c.oid)
  WHERE b.oid IN ('pg_constraint'::regclass, 'test1'::regclass)"""

FETCH_INDEX_RELFILENODES = """SELECT i.indrelid, a.oid::regclass::text, a.oid, a.relfilenode
  FROM pg_class a
    JOIN pg_index i ON (i.indexrelid = a.oid)
  WHERE a.relname IN ('pg_constraint_oid_index', 'test1x')"""

SAVE_RELFILENODES = (
    f"INSERT INTO index_relfilenodes {FETCH_TOAST_RELFILENODES};"
    f"INSERT INTO index_relfilenodes {FETCH_INDEX_RELFILENODES};"
)

COMPARE_RELFILENODES = r"""SELECT b.parent::regclass,
  regexp_replace(b.indname::text, '(pg_toast.pg_toast_)\d+(_index)', '\1<oid>\2'),
  CASE WHEN a.oid = b.indoid THEN 'OID is unchanged'
    ELSE 'OID has changed' END,
  CASE WHEN a.relfilenode = b.relfilenode THEN 'relfilenode is unchanged'
    ELSE 'relfilenode has changed' END
  FROM index_relfilenodes b
    JOIN pg_class a ON b.indname::text = a.oid::regclass::text
  ORDER BY b.parent::text, b.indname::text"""


def test_standard_options(pg_bin):
    pg_bin.check_help("reindexdb")
    pg_bin.check_version("reindexdb")
    pg_bin.check_bad_option("reindexdb")


def test_reindexdb(node, pg_bin, sql_like):
    c = node.connect()

    # Create a tablespace for testing.
    tbspace_path = node.datadir.parent / f"reindex_tbspace_{node.name}"
    tbspace_path.mkdir()
    tbspace_name = "reindex_tbspace"
    c.sql(f"CREATE TABLESPACE {tbspace_name} LOCATION '{tbspace_path.as_posix()}';")

    # Use text as data type to get a toast table.
    c.sql("CREATE TABLE test1 (a text); CREATE INDEX test1x ON test1 (a);")
    toast_table = c.sql(
        "SELECT reltoastrelid::regclass FROM pg_class WHERE oid = 'test1'::regclass;"
    )
    toast_index = c.sql(
        f"SELECT indexrelid::regclass FROM pg_index WHERE indrelid = '{toast_table}'::regclass;"
    )

    c.sql(
        "CREATE TABLE index_relfilenodes"
        " (parent regclass, indname text, indoid oid, relfilenode oid);"
    )

    c.sql(SAVE_RELFILENODES)
    sql_like(node, ["reindexdb", "postgres"], r"statement: REINDEX DATABASE postgres;")
    assert c.sql(COMPARE_RELFILENODES) == [
        ("pg_constraint", "pg_constraint_oid_index", "OID is unchanged", "relfilenode is unchanged"),
        ("pg_constraint", "pg_toast.pg_toast_<oid>_index", "OID is unchanged", "relfilenode is unchanged"),
        ("test1", "pg_toast.pg_toast_<oid>_index", "OID is unchanged", "relfilenode has changed"),
        ("test1", "test1x", "OID is unchanged", "relfilenode has changed"),
    ], "relfilenode change after REINDEX DATABASE"

    c.sql(f"TRUNCATE index_relfilenodes; {SAVE_RELFILENODES}")
    sql_like(node, ["reindexdb", "--system", "postgres"], r"statement: REINDEX SYSTEM postgres;")
    assert c.sql(COMPARE_RELFILENODES) == [
        ("pg_constraint", "pg_constraint_oid_index", "OID is unchanged", "relfilenode has changed"),
        ("pg_constraint", "pg_toast.pg_toast_<oid>_index", "OID is unchanged", "relfilenode has changed"),
        ("test1", "pg_toast.pg_toast_<oid>_index", "OID is unchanged", "relfilenode is unchanged"),
        ("test1", "test1x", "OID is unchanged", "relfilenode is unchanged"),
    ], "relfilenode change after REINDEX SYSTEM"

    sql_like(node, ["reindexdb", "--table", "test1", "postgres"],
             r"statement: REINDEX TABLE public\.test1;")
    sql_like(node, ["reindexdb", "--table", "test1", "--tablespace", tbspace_name, "postgres"],
             rf"statement: REINDEX \(TABLESPACE {tbspace_name}\) TABLE public\.test1;")
    sql_like(node, ["reindexdb", "--index", "test1x", "postgres"],
             r"statement: REINDEX INDEX public\.test1x;")
    sql_like(node, ["reindexdb", "--schema", "pg_catalog", "postgres"],
             r"statement: REINDEX SCHEMA pg_catalog;")
    sql_like(node, ["reindexdb", "--verbose", "--table", "test1", "postgres"],
             r"statement: REINDEX \(VERBOSE\) TABLE public\.test1;")
    sql_like(node, ["reindexdb", "--verbose", "--table", "test1", "--tablespace", tbspace_name, "postgres"],
             rf"statement: REINDEX \(VERBOSE, TABLESPACE {tbspace_name}\) TABLE public\.test1;")

    # Same with --concurrently.
    c.sql(f"TRUNCATE index_relfilenodes; {SAVE_RELFILENODES}")
    sql_like(node, ["reindexdb", "--concurrently", "postgres"],
             r"statement: REINDEX DATABASE CONCURRENTLY postgres;")
    assert c.sql(COMPARE_RELFILENODES) == [
        ("pg_constraint", "pg_constraint_oid_index", "OID is unchanged", "relfilenode is unchanged"),
        ("pg_constraint", "pg_toast.pg_toast_<oid>_index", "OID is unchanged", "relfilenode is unchanged"),
        ("test1", "pg_toast.pg_toast_<oid>_index", "OID has changed", "relfilenode has changed"),
        ("test1", "test1x", "OID has changed", "relfilenode has changed"),
    ], "OID change after REINDEX DATABASE CONCURRENTLY"

    sql_like(node, ["reindexdb", "--concurrently", "--table", "test1", "postgres"],
             r"statement: REINDEX TABLE CONCURRENTLY public\.test1;")
    sql_like(node, ["reindexdb", "--concurrently", "--index", "test1x", "postgres"],
             r"statement: REINDEX INDEX CONCURRENTLY public\.test1x;")
    sql_like(node, ["reindexdb", "--concurrently", "--schema", "public", "postgres"],
             r"statement: REINDEX SCHEMA CONCURRENTLY public;")
    assert pg_bin.run("reindexdb", "--concurrently", "--system", "postgres", server=node).returncode != 0
    sql_like(node, ["reindexdb", "--concurrently", "--verbose", "--table", "test1", "postgres"],
             r"statement: REINDEX \(VERBOSE\) TABLE CONCURRENTLY public\.test1;")
    sql_like(node, ["reindexdb", "--concurrently", "--verbose", "--table", "test1",
                    "--tablespace", tbspace_name, "postgres"],
             rf"statement: REINDEX \(VERBOSE, TABLESPACE {tbspace_name}\) TABLE CONCURRENTLY public\.test1;")

    # REINDEX TABLESPACE on toast indexes and tables fails.
    for args in [
        ["--table", toast_table, "--tablespace", tbspace_name, "postgres"],
        ["--concurrently", "--table", toast_table, "--tablespace", tbspace_name, "postgres"],
        ["--index", toast_index, "--tablespace", tbspace_name, "postgres"],
        ["--concurrently", "--index", toast_index, "--tablespace", tbspace_name, "postgres"],
    ]:
        pg_bin.check_all("reindexdb", *args, exit_code=1, server=node,
                         stderr=[r"cannot move system relation"])

    # connection strings
    assert pg_bin.run("reindexdb", "--echo", "--table=pg_am", "dbname=template1", server=node).returncode == 0
    assert pg_bin.run("reindexdb", "--echo", "dbname=template1", server=node).returncode == 0
    assert pg_bin.run("reindexdb", "--echo", "--system", "dbname=template1", server=node).returncode == 0

    # parallel processing
    c.sql(
        "CREATE SCHEMA s1;"
        " CREATE TABLE s1.t1(id integer);"
        " CREATE INDEX ON s1.t1(id);"
        " CREATE INDEX i1 ON s1.t1(id);"
        " CREATE SCHEMA s2;"
        " CREATE TABLE s2.t2(id integer);"
        " CREATE INDEX ON s2.t2(id);"
        " CREATE INDEX i2 ON s2.t2(id);"
        " CREATE SCHEMA s3;"
    )
    assert pg_bin.run("reindexdb", "--jobs", "2", "--system", "postgres", server=node).returncode != 0
    assert pg_bin.run(
        "reindexdb", "--jobs", "2",
        "--index", "s1.i1", "--index", "s2.i2",
        "--index", "s1.t1_id_idx", "--index", "s2.t2_id_idx",
        "postgres", server=node,
    ).returncode == 0
    sql_like(node, ["reindexdb", "--jobs", "2", "--schema", "s1", "--schema", "s2", "postgres"],
             r"statement: REINDEX TABLE s1.t1;")
    assert pg_bin.run("reindexdb", "--jobs", "2", "--schema", "s3", server=node).returncode == 0
    assert pg_bin.run("reindexdb", "--jobs", "2", "--concurrently", "--dbname", "postgres", server=node).returncode == 0

    # combinations of objects
    sql_like(node, ["reindexdb", "--system", "--table", "test1", "postgres"],
             r"statement: REINDEX SYSTEM postgres;")
    sql_like(node, ["reindexdb", "--system", "--index", "test1x", "postgres"],
             r"statement: REINDEX INDEX public.test1x;")
    sql_like(node, ["reindexdb", "--system", "--schema", "pg_catalog", "postgres"],
             r"statement: REINDEX SCHEMA pg_catalog;")
