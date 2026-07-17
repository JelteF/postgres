# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/043_no_contrecord_switch.pl.

Tests an already-propagated WAL segment that ends in an incomplete WAL record.
A record is made to span two pages (a continuation record), then the page
holding the continuation is overwritten with zeroes to simulate broken
replication. Two standbys restoring that segment from the archive must report
the empty page; after one is promoted across the page boundary, a cascading
standby must still be able to follow it.
"""

import shutil


def _int_setting(conn, name):
    return int(conn.sql(f"SELECT setting FROM pg_settings WHERE name = '{name}'"))


def test_no_contrecord_switch(create_pg):
    primary = create_pg(
        "primary",
        allows_streaming=True,
        archiving=True,
        # Minimize friction from concurrent WAL activity.
        conf={
            "autovacuum": False,
            "checkpoint_timeout": "30min",
            "wal_keep_size": "1GB",
        },
    )
    backup = primary.backup("backup")

    primary.sql("CREATE TABLE t AS SELECT 0")

    wal_segment_size = _int_setting(primary, "wal_segment_size")
    wal_block_size = _int_setting(primary, "wal_block_size")
    tli = primary.sql("SELECT timeline_id FROM pg_control_checkpoint()")

    # Get close to the end of the current WAL page, then write a record that
    # overflows it, producing a continuation record spanning two pages.
    primary.emit_wal(0)
    end_lsn = primary.advance_wal_out_of_record_splitting_zone(wal_block_size)
    overflow_size = wal_block_size - (end_lsn % wal_block_size)
    end_lsn = primary.emit_wal(overflow_size)
    primary.stop("immediate")

    # Zero out the whole page holding the continuation record to simulate
    # broken replication, and copy the "hacked" segment to the archive.
    start_page = end_lsn & ~(wal_block_size - 1)
    wal_file = primary.write_wal(
        tli, start_page, wal_segment_size, b"\x00" * wal_block_size
    )
    shutil.copy(wal_file, primary.archive_dir)

    # Two standbys that replay the hacked segment from the archive.
    standby1 = create_pg("standby1", from_backup=backup, restoring=primary, start=False)
    standby2 = create_pg("standby2", from_backup=backup, restoring=primary, start=False)
    log1 = standby1.current_log_position()
    log2 = standby2.current_log_position()
    standby1.start()
    standby2.start()

    segment = start_page // wal_segment_size
    offset = start_page % wal_segment_size
    segment_name = f"{tli:08X}{0:08X}{segment:08X}"
    pattern = rf"invalid magic number 0000 .* segment {segment_name}.* offset {offset}"

    # Both standbys complain about the empty page when assembling the record
    # that spans the two pages.
    standby1.wait_for_log(pattern, log1)
    standby2.wait_for_log(pattern, log2)

    # A promotion with a timeline jump handled at a page boundary with a
    # continuation record.
    standby1.promote()
    # Force standby2 to read a continuation record from the zeroed page.
    standby1.sql("SELECT pg_switch_wal()")
    standby1.sql("INSERT INTO t SELECT * FROM generate_series(1, 1000)")

    # standby2 streams from the promoted standby1 (and pulls WAL from the
    # archive); it should catch up.
    standby2.enable_streaming(standby1)
    standby2.pg_ctl("reload")
    standby1.wait_for_catchup(standby2)

    assert standby2.sql("SELECT count(*) FROM t") == 1001, (
        "check streamed content on standby2"
    )
