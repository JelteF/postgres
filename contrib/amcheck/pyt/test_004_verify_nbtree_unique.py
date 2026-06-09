# Copyright (c) 2023-2026, PostgreSQL Global Development Group

"""Port of contrib/amcheck/t/004_verify_nbtree_unique.pl.

Checks btree validation in the presence of breaking sort-order changes: a
custom operator class's comparison function is swapped (via pg_amproc) so that
an existing unique index becomes inconsistent, and bt_index_check is expected
to report the uniqueness / item-order-invariant violation.
"""

import pytest

from libpq import LibpqError

SETUP = [
    "CREATE EXTENSION amcheck",
    r"""
CREATE FUNCTION ok_cmp (int4, int4)
RETURNS int LANGUAGE sql AS
$$
    SELECT
        CASE WHEN $1 < $2 THEN -1
             WHEN $1 > $2 THEN  1
             ELSE 0
        END;
$$""",
    r"""
CREATE FUNCTION ok_cmp1 (int4, int4)
RETURNS int LANGUAGE sql AS
$$
    SELECT public.ok_cmp($1, $2);
$$""",
    # Make values 768 and 769 look equal.
    r"""
CREATE FUNCTION bad_cmp1 (int4, int4)
RETURNS int LANGUAGE sql AS
$$
    SELECT
        CASE WHEN ($1 = 768 AND $2 = 769) OR
                  ($1 = 769 AND $2 = 768) THEN 0
             ELSE public.ok_cmp($1, $2)
        END;
$$""",
    r"""
CREATE FUNCTION ok_cmp2 (int4, int4)
RETURNS int LANGUAGE sql AS
$$
    SELECT public.ok_cmp($1, $2);
$$""",
    r"""
CREATE FUNCTION bad_cmp2 (int4, int4)
RETURNS int LANGUAGE sql AS
$$
    SELECT
        CASE WHEN $1 = $2 AND $1 = 400 THEN -1
        ELSE public.ok_cmp($1, $2)
    END;
$$""",
    r"""
CREATE FUNCTION ok_cmp3 (int4, int4)
RETURNS int LANGUAGE sql AS
$$
    SELECT public.ok_cmp($1, $2);
$$""",
    r"""
CREATE FUNCTION bad_cmp3 (int4, int4)
RETURNS int LANGUAGE sql AS
$$
    SELECT public.bad_cmp2($1, $2);
$$""",
    "CREATE TABLE bttest_unique1 (i int4)",
    "INSERT INTO bttest_unique1 (SELECT * FROM generate_series(1, 1024) gs)",
    "CREATE TABLE bttest_unique2 (i int4)",
    "INSERT INTO bttest_unique2(i) (SELECT * FROM generate_series(1, 400) gs)",
    "INSERT INTO bttest_unique2 (SELECT * FROM generate_series(400, 1024) gs)",
    "CREATE TABLE bttest_unique3 (i int4)",
    "INSERT INTO bttest_unique3 SELECT * FROM bttest_unique2",
    """CREATE OPERATOR CLASS int4_custom_ops1 FOR TYPE int4 USING btree AS
    OPERATOR 1 < (int4, int4), OPERATOR 2 <= (int4, int4),
    OPERATOR 3 = (int4, int4), OPERATOR 4 >= (int4, int4),
    OPERATOR 5 > (int4, int4), FUNCTION 1 ok_cmp1(int4, int4)""",
    """CREATE OPERATOR CLASS int4_custom_ops2 FOR TYPE int4 USING btree AS
    OPERATOR 1 < (int4, int4), OPERATOR 2 <= (int4, int4),
    OPERATOR 3 = (int4, int4), OPERATOR 4 >= (int4, int4),
    OPERATOR 5 > (int4, int4), FUNCTION 1 bad_cmp2(int4, int4)""",
    """CREATE OPERATOR CLASS int4_custom_ops3 FOR TYPE int4 USING btree AS
    OPERATOR 1 < (int4, int4), OPERATOR 2 <= (int4, int4),
    OPERATOR 3 = (int4, int4), OPERATOR 4 >= (int4, int4),
    OPERATOR 5 > (int4, int4), FUNCTION 1 bad_cmp3(int4, int4)""",
    """CREATE UNIQUE INDEX bttest_unique_idx1 ON bttest_unique1
    USING btree (i int4_custom_ops1) WITH (deduplicate_items = off)""",
    """CREATE UNIQUE INDEX bttest_unique_idx2 ON bttest_unique2
    USING btree (i int4_custom_ops2) WITH (deduplicate_items = off)""",
    """CREATE UNIQUE INDEX bttest_unique_idx3 ON bttest_unique3
    USING btree (i int4_custom_ops3) WITH (deduplicate_items = on)""",
]

CHECK = "SELECT bt_index_check('{}', true, true)"


def test_verify_nbtree_unique(create_pg):
    node = create_pg("test", conf={"autovacuum": False})
    node.sql_batch(*SETUP)

    # Test 1: not yet broken, so no corruption.
    node.sql(CHECK.format("bttest_unique_idx1"))

    # Swap in a comparison function that treats some distinct values as equal.
    node.sql(
        "UPDATE pg_catalog.pg_amproc SET amproc = 'bad_cmp1'::regproc "
        "WHERE amproc = 'ok_cmp1'::regproc"
    )
    # A raw pg_amproc UPDATE (unlike ALTER OPERATOR FAMILY) doesn't send a
    # relcache invalidation for indexes using that opclass, so the check must
    # run on a fresh session to rebuild the index's cached support function.
    with pytest.raises(
        LibpqError, match='index uniqueness is violated for index "bttest_unique_idx1"'
    ):
        node.sql_oneshot(CHECK.format("bttest_unique_idx1"))

    # Test 2: index built under a bad cmp; first an item-order violation, then a
    # uniqueness violation once the comparison function is corrected.
    with pytest.raises(
        LibpqError,
        match='item order invariant violated for index "bttest_unique_idx2"',
    ):
        node.sql_oneshot(CHECK.format("bttest_unique_idx2"))

    node.sql(
        "UPDATE pg_catalog.pg_amproc SET amproc = 'ok_cmp2'::regproc "
        "WHERE amproc = 'bad_cmp2'::regproc"
    )
    with pytest.raises(
        LibpqError, match='index uniqueness is violated for index "bttest_unique_idx2"'
    ):
        node.sql_oneshot(CHECK.format("bttest_unique_idx2"))

    # Test 3: same as test 2 but with deduplication on.
    with pytest.raises(
        LibpqError,
        match='item order invariant violated for index "bttest_unique_idx3"',
    ):
        node.sql_oneshot(CHECK.format("bttest_unique_idx3"))

    # Create posting-list entries with equal values but different visibility,
    # so deduplication kicks in for the unique index.
    node.sql_batch(
        "DELETE FROM bttest_unique3 WHERE 380 <= i AND i <= 420",
        "INSERT INTO bttest_unique3 (SELECT * FROM generate_series(380, 420))",
        "INSERT INTO bttest_unique3 VALUES (400)",
        "DELETE FROM bttest_unique3 WHERE 380 <= i AND i <= 420",
        "INSERT INTO bttest_unique3 (SELECT * FROM generate_series(380, 420))",
        "INSERT INTO bttest_unique3 VALUES (400)",
        "DELETE FROM bttest_unique3 WHERE 380 <= i AND i <= 420",
        "INSERT INTO bttest_unique3 (SELECT * FROM generate_series(380, 420))",
        "INSERT INTO bttest_unique3 VALUES (400)",
    )
    node.sql(
        "UPDATE pg_catalog.pg_amproc SET amproc = 'ok_cmp3'::regproc "
        "WHERE amproc = 'bad_cmp3'::regproc"
    )
    with pytest.raises(
        LibpqError, match='index uniqueness is violated for index "bttest_unique_idx3"'
    ):
        node.sql_oneshot(CHECK.format("bttest_unique_idx3"))
