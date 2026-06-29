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

    # These statements all run on one server with no bounce in between, so
    # share a single connection. The in-place tablespaces need the SET to
    # persist to the CREATE anyway, which the held connection provides.
    c = node.connect()

    # Create a tablespace with an absolute path.
    c.sql(f"CREATE TABLESPACE regress_ts1 LOCATION '{ts1}'")
    # Can't create a tablespace where there is one already.
    with pytest.raises(LibpqError, match='tablespace "regress_ts1" already exists'):
        c.sql(f"CREATE TABLESPACE regress_ts1 LOCATION '{ts1}'")
    # Create a table in it.
    c.sql("CREATE TABLE t () TABLESPACE regress_ts1")
    # Can't drop a tablespace that still has a table in it.
    with pytest.raises(LibpqError, match='tablespace "regress_ts1" is not empty'):
        c.sql("DROP TABLESPACE regress_ts1")
    c.sql("DROP TABLE t")
    c.sql("DROP TABLESPACE regress_ts1")

    # Create two absolute and two in-place tablespaces to test moves.
    c.sql(f"CREATE TABLESPACE regress_ts1 LOCATION '{ts1}'")
    c.sql(f"CREATE TABLESPACE regress_ts2 LOCATION '{ts2}'")
    # CREATE TABLESPACE can't run in a transaction block, so the statements are
    # issued separately rather than as one string.
    c.sql("SET allow_in_place_tablespaces=on")
    c.sql("CREATE TABLESPACE regress_ts3 LOCATION ''")
    c.sql("CREATE TABLESPACE regress_ts4 LOCATION ''")

    # Create a table and move it between absolute and in-place tablespaces.
    c.sql("CREATE TABLE t () TABLESPACE regress_ts1")
    c.sql("ALTER TABLE t SET tablespace regress_ts2")
    c.sql("ALTER TABLE t SET tablespace regress_ts3")
    c.sql("ALTER TABLE t SET tablespace regress_ts4")
    c.sql("ALTER TABLE t SET tablespace regress_ts1")

    c.sql("DROP TABLE t")
    c.sql("DROP TABLESPACE regress_ts1")
    c.sql("DROP TABLESPACE regress_ts2")
    c.sql("DROP TABLESPACE regress_ts3")
    c.sql("DROP TABLESPACE regress_ts4")

    c.close()
    node.stop()
