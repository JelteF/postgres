# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/commit_ts/t/001_base.pl.

Single-node test: a commit timestamp can be read, and is still present after
crash recovery.
"""


def test_commit_ts_survives_recovery(create_pg):
    node = create_pg("committs_base", conf={"track_commit_timestamp": True})

    # Create a table, compare now() to the commit TS of its xmin.
    node.sql("create table t as select now from (select now(), pg_sleep(1)) f")
    assert (
        node.sql(
            "select t.now - ts.* < '1s' from t, pg_class c,"
            " pg_xact_commit_timestamp(c.xmin) ts where relname = 't'"
        )
        is True
    )
    ts = node.sql(
        "select ts.* from pg_class, pg_xact_commit_timestamp(xmin) ts"
        " where relname = 't'"
    )

    # Verify that we read the same TS after crash recovery.
    node.stop("immediate")
    node.start()
    recovered_ts = node.sql(
        "select ts.* from pg_class, pg_xact_commit_timestamp(xmin) ts"
        " where relname = 't'"
    )
    assert recovered_ts == ts
