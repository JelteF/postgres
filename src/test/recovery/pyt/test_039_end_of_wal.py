# Copyright (c) 2023-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/039_end_of_wal.pl.

Test detecting end-of-WAL conditions by writing fake defective page and record
headers into a stopped server's WAL, then restarting and checking that recovery
reports the expected end-of-WAL diagnostic for each scenario.
"""

import pathlib
import re
import struct

from pypg.bins import pg_config


def _scan_server_header(header_path, pattern):
    """Return the first capture of ``pattern`` in an installed server header,
    mirroring Perl's scan_server_header()."""
    includedir = pg_config.capture("--includedir-server")
    text = pathlib.Path(includedir, header_path).read_text()
    m = re.search(rf"^{pattern}", text, re.M)
    assert m, f"could not find match in header {header_path}"
    return m.group(1)


def _build_record_header(
    xl_tot_len, xl_xid=0, xl_prev=0, xl_info=0, xl_rmid=0, xl_crc=0
):
    """Pack a fake XLogRecord header (24 bytes) for write_wal()."""
    # uint32 tot_len, uint32 xid, uint64 prev, uint8 info, uint8 rmid,
    # 2 bytes padding, uint32 crc.
    return struct.pack(
        "=IIQBBBBI", xl_tot_len, xl_xid, xl_prev, xl_info, xl_rmid, 0, 0, xl_crc
    )


def _build_page_header(xlp_magic, xlp_info=0, xlp_tli=0, xlp_pageaddr=0, xlp_rem_len=0):
    """Pack the first 20 bytes of an XLogPageHeaderData for write_wal()."""
    # uint16 magic, uint16 info, uint32 tli, uint64 pageaddr, uint32 rem_len.
    return struct.pack(
        "=HHIQI", xlp_magic, xlp_info, xlp_tli, xlp_pageaddr, xlp_rem_len
    )


def test_end_of_wal(create_pg):
    page_magic = int(
        _scan_server_header(
            "access/xlog_internal.h", r"#define\s+XLOG_PAGE_MAGIC\s+(\w+)"
        ),
        16,
    )
    first_is_contrecord = int(
        _scan_server_header(
            "access/xlog_internal.h", r"#define\s+XLP_FIRST_IS_CONTRECORD\s+(\w+)"
        ),
        16,
    )

    # Minimize arbitrary records: wal_level=minimal avoids standby snapshots,
    # autovacuum off and a long checkpoint_timeout avoid background WAL.
    node = create_pg(
        "node",
        conf={
            "wal_level": "minimal",
            "max_wal_senders": 0,
            "autovacuum": False,
            "checkpoint_timeout": "30min",
        },
    )
    node.sql("CREATE TABLE t AS SELECT 42")

    def int_setting(name):
        return int(node.sql(f"SELECT setting FROM pg_settings WHERE name = '{name}'"))

    wal_segment_size = int_setting("wal_segment_size")
    wal_block_size = int_setting("wal_block_size")
    tli = node.sql("SELECT timeline_id FROM pg_control_checkpoint()")

    # Initial LSN varies by initdb; switch to a fresh WAL file so all systems
    # start in the same place. The first test depends on trailing zeroes on a
    # page with a valid header.
    node.sql("SELECT pg_switch_wal()")

    def start_of_next_page(lsn):
        return (lsn & ~(wal_block_size - 1)) + wal_block_size

    def expect_recovery_log(pattern, *writes):
        """Stop the node, splice the given (lsn, bytes) writes into its WAL,
        restart, and wait for the recovery diagnostic to appear in the log."""
        node.stop("immediate")
        for lsn, data in writes:
            node.write_wal(tli, lsn, wal_segment_size, data)
        log_size = node.current_log_position()
        node.start()
        node.wait_for_log(pattern, log_size)

    # --- Single-page end-of-WAL detection ---

    # xl_tot_len is 0 (the common case: trailing zeroes).
    node.emit_wal(0)
    node.advance_wal_out_of_record_splitting_zone(wal_block_size)
    expect_recovery_log("invalid record length at .*: expected at least 24, got 0")

    # xl_tot_len is < 24 (recycled garbage).
    node.emit_wal(0)
    end_lsn = node.advance_wal_out_of_record_splitting_zone(wal_block_size)
    expect_recovery_log(
        "invalid record length at .*: expected at least 24, got 23",
        (end_lsn, _build_record_header(23)),
    )

    # xl_tot_len in final position: too small to span a new page but not
    # eligible for regular record-header validation.
    node.emit_wal(0)
    end_lsn = node.advance_wal_to_record_splitting_zone(wal_block_size)
    expect_recovery_log(
        "invalid record length at .*: expected at least 24, got 1",
        (end_lsn, _build_record_header(1)),
    )

    # Need more pages, but the xl_prev check fails first.
    node.emit_wal(0)
    end_lsn = node.advance_wal_out_of_record_splitting_zone(wal_block_size)
    expect_recovery_log(
        "record with incorrect prev-link 0/DEADBEEF at .*",
        (end_lsn, _build_record_header(2 * 1024 * 1024 * 1024, 0, 0xDEADBEEF)),
    )

    # xl_crc check fails.
    node.emit_wal(0)
    node.advance_wal_out_of_record_splitting_zone(wal_block_size)
    end_lsn = node.emit_wal(10)
    # Corrupt a byte in that record, breaking its CRC.
    expect_recovery_log(
        "incorrect resource manager data checksum in record at .*",
        (end_lsn - 8, b"!"),
    )

    # --- Multi-page end-of-WAL detection, record header not split ---
    # These need a valid xl_prev in the record header.

    def multipage_prev_setup():
        node.emit_wal(0)
        prev = node.advance_wal_out_of_record_splitting_zone(wal_block_size)
        end = node.emit_wal(0)
        return prev, end

    # Good xl_prev, we hit a zero page next (zero magic).
    prev_lsn, end_lsn = multipage_prev_setup()
    expect_recovery_log(
        "invalid magic number 0000 .* LSN .*",
        (end_lsn, _build_record_header(2 * 1024 * 1024 * 1024, 0, prev_lsn)),
    )

    # Good xl_prev, we hit a garbage page next (bad magic).
    prev_lsn, end_lsn = multipage_prev_setup()
    expect_recovery_log(
        "invalid magic number CAFE .* LSN .*",
        (end_lsn, _build_record_header(2 * 1024 * 1024 * 1024, 0, prev_lsn)),
        (start_of_next_page(end_lsn), _build_page_header(0xCAFE, 0, 1, 0)),
    )

    # Good xl_prev, hit a typical recycled page (good magic, bad pageaddr).
    prev_lsn, end_lsn = multipage_prev_setup()
    expect_recovery_log(
        "unexpected pageaddr 0/BAAAAAAD in .*, LSN .*,",
        (end_lsn, _build_record_header(2 * 1024 * 1024 * 1024, 0, prev_lsn)),
        (start_of_next_page(end_lsn), _build_page_header(page_magic, 0, 1, 0xBAAAAAAD)),
    )

    # Good xl_prev/magic/pageaddr, but bogus xlp_info.
    prev_lsn, end_lsn = multipage_prev_setup()
    expect_recovery_log(
        "invalid info bits 1234 in .*, LSN .*,",
        (end_lsn, _build_record_header(2 * 1024 * 1024 * 1024, 42, prev_lsn)),
        (
            start_of_next_page(end_lsn),
            _build_page_header(page_magic, 0x1234, 1, start_of_next_page(end_lsn)),
        ),
    )

    # Good xl_prev/magic/pageaddr, but xlp_info lacks the contrecord flag.
    prev_lsn, end_lsn = multipage_prev_setup()
    expect_recovery_log(
        "there is no contrecord flag at .*",
        (end_lsn, _build_record_header(2 * 1024 * 1024 * 1024, 42, prev_lsn)),
        (
            start_of_next_page(end_lsn),
            _build_page_header(page_magic, 0, 1, start_of_next_page(end_lsn)),
        ),
    )

    # Good xl_prev/magic/pageaddr/info, but xlp_rem_len doesn't add up.
    prev_lsn, end_lsn = multipage_prev_setup()
    expect_recovery_log(
        "invalid contrecord length 123456 .* at .*",
        (end_lsn, _build_record_header(2 * 1024 * 1024 * 1024, 42, prev_lsn)),
        (
            start_of_next_page(end_lsn),
            _build_page_header(
                page_magic, first_is_contrecord, 1, start_of_next_page(end_lsn), 123456
            ),
        ),
    )

    # --- Multi-page, record header split, so page checks happen first ---

    # xl_prev is bad and xl_tot_len too big, but xlp_magic is checked first.
    node.emit_wal(0)
    end_lsn = node.advance_wal_to_record_splitting_zone(wal_block_size)
    expect_recovery_log(
        "invalid magic number 0000 .* LSN .*",
        (end_lsn, _build_record_header(2 * 1024 * 1024 * 1024, 0, 0xDEADBEEF)),
    )

    # xlp_pageaddr is checked before any header checks too.
    node.emit_wal(0)
    end_lsn = node.advance_wal_to_record_splitting_zone(wal_block_size)
    expect_recovery_log(
        "unexpected pageaddr 0/BAAAAAAD in .*, LSN .*,",
        (end_lsn, _build_record_header(2 * 1024 * 1024 * 1024, 0, 0xDEADBEEF)),
        (
            start_of_next_page(end_lsn),
            _build_page_header(page_magic, first_is_contrecord, 1, 0xBAAAAAAD),
        ),
    )

    # xlp_rem_len is found not to add up before any header checks.
    node.emit_wal(0)
    end_lsn = node.advance_wal_to_record_splitting_zone(wal_block_size)
    expect_recovery_log(
        "invalid contrecord length 123456 .* at .*",
        (end_lsn, _build_record_header(2 * 1024 * 1024 * 1024, 0, 0xDEADBEEF)),
        (
            start_of_next_page(end_lsn),
            _build_page_header(
                page_magic, first_is_contrecord, 1, start_of_next_page(end_lsn), 123456
            ),
        ),
    )
