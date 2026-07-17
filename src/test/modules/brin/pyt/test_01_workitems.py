# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/brin/t/01_workitems.pl.

Verifies that BRIN autosummarization work items run: after inserting data,
autovacuum summarizes the ranges of autosummarizing BRIN indexes.
"""


def test_workitems(create_pg):
    node = create_pg("brin_wi", conf={"autovacuum_naptime": "1s"})

    node.sql("create extension pageinspect")

    # A table with an autosummarizing BRIN index, ...
    node.sql("create table brin_wi (a int) with (fillfactor = 10)")
    node.sql(
        "create index brin_wi_idx on brin_wi using brin (a) "
        "with (pages_per_range=1, autosummarize=on)"
    )
    # ... and one whose index needs a snapshot to run.
    node.sql("create table journal (d timestamp) with (fillfactor = 10)")
    node.sql(
        "create function packdate(d timestamp) returns text language plpgsql "
        "as $$ begin return to_char(d, 'yyyymm'); end; $$ "
        "returns null on null input immutable"
    )
    node.sql(
        "create index brin_packdate_idx on journal using brin (packdate(d)) "
        "with (autosummarize = on, pages_per_range = 1)"
    )

    def page_items(idx, where=""):
        return node.sql(
            f"select count(*) from brin_page_items(get_raw_page('{idx}', 2), "
            f"'{idx}'::regclass) {where}"
        )

    assert page_items("brin_wi_idx") == 1, "initial brin_wi_idx state"
    assert page_items("brin_packdate_idx") == 1, "initial brin_packdate_idx state"

    node.sql("insert into brin_wi select * from generate_series(1, 100)")
    node.sql(
        "insert into journal select * from "
        "generate_series(timestamp '1976-08-01', '1976-10-28', '1 day')"
    )

    # Autovacuum summarizes the new ranges.
    node.poll_query_until(
        "select count(*) > 1 from brin_page_items(get_raw_page('brin_wi_idx', 2), "
        "'brin_wi_idx'::regclass)"
    )
    assert page_items("brin_wi_idx", "where not placeholder") > 1

    node.poll_query_until(
        "select count(*) > 1 from brin_page_items("
        "get_raw_page('brin_packdate_idx', 2), 'brin_packdate_idx'::regclass)"
    )
    assert page_items("brin_packdate_idx", "where not placeholder") > 1

    node.stop()
