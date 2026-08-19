# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/026_overwrite_contrecord.pl.

Creates a physical replica that is missing the last WAL file, then restarts the
primary so it writes a divergent WAL file containing an "overwrite contrecord".
The replica must replay past that record (and report skipping the missing
contrecord), then promote successfully.
"""

# Fill the current WAL segment, leaving room only for the start of a largish
# record, so the following big message's contrecord spills into the next file.
FILL_WAL = r"""
DO $$
DECLARE
    wal_segsize int := setting::int FROM pg_settings WHERE name = 'wal_segment_size';
    remain int;
    iters  int := 0;
BEGIN
    LOOP
        INSERT into filler
        select g, repeat(encode(sha256(g::text::bytea), 'hex'), (random() * 15 + 1)::int)
        from generate_series(1, 10) g;

        remain := wal_segsize - (pg_current_wal_insert_lsn() - '0/0') % wal_segsize;
        IF remain < 2 * setting::int from pg_settings where name = 'block_size' THEN
            RAISE log 'exiting after % iterations, % bytes to end of WAL segment', iters, remain;
            EXIT;
        END IF;
        iters := iters + 1;
    END LOOP;
END
$$;
"""


def test_overwrite_contrecord(create_pg):
    node = create_pg(
        "primary",
        allows_streaming=True,
        conf={"autovacuum": False, "wal_keep_size": "1GB"},
    )
    node.sql("create table filler (a int, b text)")
    node.sql(FILL_WAL)

    initfile = node.sql("SELECT pg_walfile_name(pg_current_wal_insert_lsn())")
    node.sql(
        "SELECT pg_logical_emit_message(true, 'test 026', repeat('xyzxz', 123456))"
    )
    endfile = node.sql("SELECT pg_walfile_name(pg_current_wal_insert_lsn())")
    assert initfile != endfile, f"{initfile} differs from {endfile}"

    # Stop abruptly (no shutdown checkpoint), then remove the tail file; on
    # restart the large message will be overwritten with new contents.
    node.stop("immediate")
    (node.datadir / "pg_wal" / endfile).unlink()

    # A standby from this point, started before the primary comes back.
    backup = node.backup_fs_cold("backup")
    standby = create_pg("standby", from_backup=backup, streaming_primary=node)
    node.start()

    node.sql_batch("create table foo (a text)", "insert into foo values ('hello')")
    node.sql("SELECT pg_logical_emit_message(true, 'test 026', 'AABBCC')")

    until_lsn = node.lsn("write")
    standby.poll_query_until(
        f"SELECT '{until_lsn}'::pg_lsn <= pg_last_wal_replay_lsn()"
    )

    assert standby.sql("select * from foo") == "hello", (
        "standby replays past overwritten contrecord"
    )
    assert "successfully skipped missing contrecord at" in standby.log_since(0), (
        "found log line in standby"
    )

    standby.promote()
