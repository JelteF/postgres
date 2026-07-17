# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/test_misc/t/002_tablespace.pl.

Simple tablespace tests using absolute paths, kept out of the regular
regression suite because the paths can't be replicated on the same host.
"""

import pytest

from libpq import LibpqError


def test_tablespace(create_pg):
    node = create_pg("tablespace")

    basedir = node.datadir.parent
    ts1 = (basedir / "ts1").as_posix()
    ts2 = (basedir / "ts2").as_posix()
    (basedir / "ts1").mkdir()
    (basedir / "ts2").mkdir()

    # Create a tablespace with an absolute path.
    node.sql(f"CREATE TABLESPACE regress_ts1 LOCATION '{ts1}'")
    # Can't create a tablespace where there is one already.
    with pytest.raises(LibpqError, match='tablespace "regress_ts1" already exists'):
        node.sql(f"CREATE TABLESPACE regress_ts1 LOCATION '{ts1}'")
    # Create a table in it.
    node.sql("CREATE TABLE t () TABLESPACE regress_ts1")
    # Can't drop a tablespace that still has a table in it.
    with pytest.raises(LibpqError, match='tablespace "regress_ts1" is not empty'):
        node.sql("DROP TABLESPACE regress_ts1")
    node.sql("DROP TABLE t")
    node.sql("DROP TABLESPACE regress_ts1")

    # Create two absolute and two in-place tablespaces to test moves.
    node.sql(f"CREATE TABLESPACE regress_ts1 LOCATION '{ts1}'")
    node.sql(f"CREATE TABLESPACE regress_ts2 LOCATION '{ts2}'")
    # CREATE TABLESPACE can't run in a transaction block, so the statements are
    # issued separately rather than as one string.
    node.sql("SET allow_in_place_tablespaces=on")
    node.sql("CREATE TABLESPACE regress_ts3 LOCATION ''")
    node.sql("CREATE TABLESPACE regress_ts4 LOCATION ''")

    # Create a table and move it between absolute and in-place tablespaces.
    node.sql("CREATE TABLE t () TABLESPACE regress_ts1")
    node.sql("ALTER TABLE t SET tablespace regress_ts2")
    node.sql("ALTER TABLE t SET tablespace regress_ts3")
    node.sql("ALTER TABLE t SET tablespace regress_ts4")
    node.sql("ALTER TABLE t SET tablespace regress_ts1")

    node.sql("DROP TABLE t")
    node.sql("DROP TABLESPACE regress_ts1")
    node.sql("DROP TABLESPACE regress_ts2")
    node.sql("DROP TABLESPACE regress_ts3")
    node.sql("DROP TABLESPACE regress_ts4")

    node.stop()
