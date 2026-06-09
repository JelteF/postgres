# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/brin/t/02_wal_consistency.pl.

Verifies WAL consistency for BRIN: with wal_consistency_checking = brin, a
streaming standby replays BRIN WAL (including revmap extension) and the
consistency check finds no mismatch.
"""

LOOP_SQL = """
do
$$
declare
  current timestamp with time zone := '2019-03-27 08:14:01.123456789 UTC';
begin
  loop
    insert into tbl_timestamp0 select i from
      generate_series(current, current + interval '1 day', '28 seconds') i;
    perform brin_summarize_new_values('tbl_timestamp0_d1_idx');
    if (brin_metapage_info(get_raw_page('tbl_timestamp0_d1_idx', 0))).lastrevmappage > 1 then
      exit;
    end if;
    current := current + interval '1 day';
  end loop;
end
$$;
"""


def test_wal_consistency(create_pg):
    primary = create_pg(
        "brin_whiskey", allows_streaming=True, conf={"wal_consistency_checking": "brin"}
    )
    primary.sql("create extension pageinspect")
    primary.sql("create extension pg_walinspect")
    primary.sql("SELECT pg_create_physical_replication_slot('standby_1')")

    backup = primary.backup("brinbkp")

    # The standby inherits wal_consistency_checking from the backup, so the
    # consistency check runs as it replays the primary's BRIN WAL.
    standby = create_pg(
        "brin_charlie",
        from_backup=backup,
        streaming_primary=primary,
        conf={"primary_slot_name": "standby_1"},
    )

    primary.sql(
        "create table tbl_timestamp0 (d1 timestamp(0) without time zone) "
        "with (fillfactor=10)"
    )
    primary.sql(
        "create index on tbl_timestamp0 using brin (d1) "
        "with (pages_per_range = 1, autosummarize=false)"
    )

    start_lsn = primary.lsn("insert")
    # Insert until a second revmap page is created.
    primary.sql(LOOP_SQL)
    end_lsn = primary.lsn("flush")

    # The WAL between the two LSNs contains BRIN revmap records.
    count = primary.sql(
        f"select count(*) from pg_get_wal_records_info('{start_lsn}', '{end_lsn}') "
        "where resource_manager = 'BRIN' AND record_type ILIKE '%revmap%'"
    )
    assert count >= 1

    # The standby replays it all without a consistency-check failure.
    primary.wait_for_catchup(standby)
