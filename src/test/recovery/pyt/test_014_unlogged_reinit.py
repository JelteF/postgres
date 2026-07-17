# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/014_unlogged_reinit.pl.

Tests that unlogged relations are properly reinitialized after a crash: the
main fork is recreated from the init fork, while stray vm/fsm forks are
removed. Covers unlogged tables and sequences both in the default tablespace
and in a user tablespace, including sequences whose relation was rewritten by
ALTER SEQUENCE / TRUNCATE RESTART IDENTITY.
"""

import pathlib


def test_unlogged_reinit(create_pg):
    node = create_pg("main")
    pgdata = pathlib.Path(node.datadir)

    # An unlogged table and sequence, to check that forks other than init are
    # not preserved across the crash.
    node.sql("CREATE UNLOGGED TABLE base_unlogged (id int)")
    node.sql("CREATE UNLOGGED SEQUENCE seq_unlogged")

    base_table = node.sql("SELECT pg_relation_filepath('base_unlogged')")
    base_seq = node.sql("SELECT pg_relation_filepath('seq_unlogged')")

    assert (pgdata / f"{base_table}_init").is_file(), "table init fork exists"
    assert (pgdata / base_table).is_file(), "table main fork exists"
    assert (pgdata / f"{base_seq}_init").is_file(), "sequence init fork exists"
    assert (pgdata / base_seq).is_file(), "sequence main fork exists"

    assert node.sql("SELECT nextval('seq_unlogged')") == 1, "sequence nextval"
    assert node.sql("SELECT nextval('seq_unlogged')") == 2, "sequence nextval again"

    # An unlogged table in a user tablespace. Put the tablespace directory
    # under the data directory's parent rather than pytest's tmp_path: on
    # Windows the (privilege-dropped) postmaster must be able to set
    # permissions on it, and the CI grants the needed ACLs on the test tree
    # but not on the system temp directory.
    ts_dir = node.datadir.parent / "ts1"
    ts_dir.mkdir()
    node.sql(f"CREATE TABLESPACE ts1 LOCATION '{ts_dir}'")
    node.sql("CREATE UNLOGGED TABLE ts1_unlogged (id int) TABLESPACE ts1")

    ts1_table = node.sql("SELECT pg_relation_filepath('ts1_unlogged')")
    assert (pgdata / f"{ts1_table}_init").is_file(), "init fork in tablespace exists"
    assert (pgdata / ts1_table).is_file(), "main fork in tablespace exists"

    # Sequences whose relation gets rewritten, to exercise reinit of those.
    node.sql("CREATE UNLOGGED SEQUENCE seq_unlogged2")
    node.sql("ALTER SEQUENCE seq_unlogged2 INCREMENT 2")  # rewrites in AlterSequence()
    node.sql("SELECT nextval('seq_unlogged2')")

    node.sql(
        "CREATE UNLOGGED TABLE tab_seq_unlogged3 (a int GENERATED ALWAYS AS IDENTITY)"
    )
    node.sql(
        "TRUNCATE tab_seq_unlogged3 RESTART IDENTITY"
    )  # rewrites in ResetSequence()
    node.sql("INSERT INTO tab_seq_unlogged3 DEFAULT VALUES")

    node.stop("immediate")

    # Stray vm/fsm forks must be removed during recovery; the main fork must be
    # recopied from the init fork after we delete it.
    (pgdata / f"{base_table}_vm").write_text("TEST_VM")
    (pgdata / f"{base_table}_fsm").write_text("TEST_FSM")
    (pgdata / base_table).unlink()
    (pgdata / base_seq).unlink()

    (pgdata / f"{ts1_table}_vm").write_text("TEST_VM")
    (pgdata / f"{ts1_table}_fsm").write_text("TEST_FSM")
    (pgdata / ts1_table).unlink()

    node.start()

    assert (pgdata / f"{base_table}_init").is_file(), (
        "table init fork in base still exists"
    )
    assert (pgdata / base_table).is_file(), (
        "table main fork in base recreated at startup"
    )
    assert not (pgdata / f"{base_table}_vm").exists(), (
        "vm fork in base removed at startup"
    )
    assert not (pgdata / f"{base_table}_fsm").exists(), (
        "fsm fork in base removed at startup"
    )

    assert (pgdata / f"{base_seq}_init").is_file(), "sequence init fork still exists"
    assert (pgdata / base_seq).is_file(), "sequence main fork recreated at startup"

    assert node.sql("SELECT nextval('seq_unlogged')") == 1, (
        "sequence nextval after restart"
    )
    assert node.sql("SELECT nextval('seq_unlogged')") == 2, (
        "sequence nextval after restart again"
    )

    assert (pgdata / f"{ts1_table}_init").is_file(), (
        "init fork still exists in tablespace"
    )
    assert (pgdata / ts1_table).is_file(), (
        "main fork in tablespace recreated at startup"
    )
    assert not (pgdata / f"{ts1_table}_vm").exists(), (
        "vm fork in tablespace removed at startup"
    )
    assert not (pgdata / f"{ts1_table}_fsm").exists(), (
        "fsm fork in tablespace removed at startup"
    )

    assert node.sql("SELECT nextval('seq_unlogged2')") == 1, (
        "altered sequence nextval after restart"
    )
    assert node.sql("SELECT nextval('seq_unlogged2')") == 3, (
        "altered sequence nextval after restart again"
    )

    node.sql("INSERT INTO tab_seq_unlogged3 VALUES (DEFAULT), (DEFAULT)")
    assert node.sql("SELECT * FROM tab_seq_unlogged3") == [1, 2], (
        "reset sequence nextval after restart"
    )
