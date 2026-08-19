# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/test_misc/t/001_constraint_validation.pl.

Verifies that ALTER TABLE skips the table-rewrite/verification scan when
existing constraints already prove the new constraint holds. The cluster runs
with client_min_messages = DEBUG1 so the backend's "verifying table" and
"... is implied by existing constraints" debug messages reach the client; the
notice receiver surfaces them as Python warnings, which we collect and inspect.
"""

import warnings


def test_constraint_validation(create_pg):
    # client_min_messages = DEBUG1 makes the backend emit the debug messages
    # this test inspects to the client.
    node = create_pg("primary", conf={"client_min_messages": "DEBUG1"})

    def run_sql(*statements):
        """Run SQL and return the server's debug/notice messages as text, the
        equivalent of capturing psql's stderr."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            node.sql_batch(*statements)
        return "\n".join(str(w.message) for w in caught)

    def is_table_verified(output):
        return "DEBUG:  verifying table" in output

    # --- alter table set not null ---

    run_sql(
        "create table atacc1 (test_a int, test_b int)",
        "insert into atacc1 values (1, 2)",
    )

    output = run_sql("alter table atacc1 alter test_a set not null;")
    assert is_table_verified(output), "column test_a without constraint will scan table"

    run_sql(
        "alter table atacc1 alter test_a drop not null",
        "alter table atacc1 add constraint atacc1_constr_a_valid "
        "check(test_a is not null)",
    )

    # Normal run will verify table data.
    output = run_sql("alter table atacc1 alter test_a set not null;")
    assert not is_table_verified(output), "with constraint will not scan table"
    assert (
        'existing constraints on column "atacc1.test_a" are sufficient to prove '
        "that it does not contain nulls" in output
    )

    run_sql("alter table atacc1 alter test_a drop not null;")

    # Only test_a has a check, so test_b still forces a table scan.
    output = run_sql(
        "alter table atacc1 alter test_b set not null, alter test_a set not null;"
    )
    assert is_table_verified(output), "table was scanned"
    # We may miss the debug message for the test_a constraint because the table
    # is verified anyway due to test_b.
    assert (
        'existing constraints on column "atacc1.test_b" are sufficient to prove '
        "that it does not contain nulls" not in output
    )
    run_sql(
        "alter table atacc1 alter test_a drop not null, alter test_b drop not null;"
    )

    # Both columns have check constraints.
    run_sql(
        "alter table atacc1 add constraint atacc1_constr_b_valid "
        "check(test_b is not null)"
    )
    output = run_sql(
        "alter table atacc1 alter test_b set not null, alter test_a set not null;"
    )
    assert not is_table_verified(output), "table was not scanned for both columns"
    assert (
        'existing constraints on column "atacc1.test_a" are sufficient to prove '
        "that it does not contain nulls" in output
    )
    assert (
        'existing constraints on column "atacc1.test_b" are sufficient to prove '
        "that it does not contain nulls" in output
    )
    run_sql("drop table atacc1;")

    # --- alter table attach partition ---

    run_sql(
        "CREATE TABLE list_parted2 (a int, b char) PARTITION BY LIST (a)",
        "CREATE TABLE part_3_4 (LIKE list_parted2, "
        "CONSTRAINT check_a CHECK (a IN (3)))",
    )

    # Needs NOT NULL to skip the table scan.
    output = run_sql(
        "ALTER TABLE list_parted2 ATTACH PARTITION part_3_4 FOR VALUES IN (3, 4);"
    )
    assert is_table_verified(output), "table part_3_4 scanned"

    run_sql(
        "ALTER TABLE list_parted2 DETACH PARTITION part_3_4",
        "ALTER TABLE part_3_4 ALTER a SET NOT NULL",
    )

    output = run_sql(
        "ALTER TABLE list_parted2 ATTACH PARTITION part_3_4 FOR VALUES IN (3, 4);"
    )
    assert not is_table_verified(output), "table part_3_4 not scanned"
    assert (
        'partition constraint for table "part_3_4" is implied by existing constraints'
        in output
    )

    # Attach default partition.
    run_sql(
        "CREATE TABLE list_parted2_def (LIKE list_parted2, "
        "CONSTRAINT check_a CHECK (a IN (5, 6)));"
    )
    output = run_sql(
        "ALTER TABLE list_parted2 ATTACH PARTITION list_parted2_def default;"
    )
    assert not is_table_verified(output), "table list_parted2_def not scanned"
    assert (
        'partition constraint for table "list_parted2_def" is implied by existing '
        "constraints" in output
    )

    output = run_sql(
        "CREATE TABLE part_55_66 PARTITION OF list_parted2 FOR VALUES IN (55, 66);"
    )
    assert not is_table_verified(output), "table list_parted2_def not scanned"
    assert (
        'updated partition constraint for default partition "list_parted2_def" is '
        "implied by existing constraints" in output
    )

    # Attach another partitioned table.
    run_sql(
        "CREATE TABLE part_5 (LIKE list_parted2) PARTITION BY LIST (b)",
        "CREATE TABLE part_5_a PARTITION OF part_5 FOR VALUES IN ('a')",
        "ALTER TABLE part_5 ADD CONSTRAINT check_a CHECK (a IS NOT NULL AND a = 5)",
    )
    output = run_sql(
        "ALTER TABLE list_parted2 ATTACH PARTITION part_5 FOR VALUES IN (5);"
    )
    assert 'verifying table "part_5"' not in output, "table part_5 not scanned"
    assert 'verifying table "list_parted2_def"' in output, "list_parted2_def scanned"
    assert (
        'partition constraint for table "part_5" is implied by existing constraints'
        in output
    )

    run_sql(
        "ALTER TABLE list_parted2 DETACH PARTITION part_5",
        "ALTER TABLE part_5 DROP CONSTRAINT check_a",
    )

    # Scan should again be skipped, even though NOT NULL is now a column property.
    run_sql(
        "ALTER TABLE part_5 ADD CONSTRAINT check_a CHECK (a IN (5)), "
        "ALTER a SET NOT NULL;"
    )
    output = run_sql(
        "ALTER TABLE list_parted2 ATTACH PARTITION part_5 FOR VALUES IN (5);"
    )
    assert 'verifying table "part_5"' not in output, "table part_5 not scanned"
    assert 'verifying table "list_parted2_def"' in output, "list_parted2_def scanned"
    assert (
        'partition constraint for table "part_5" is implied by existing constraints'
        in output
    )

    # attnos of the partitioning columns in the attached table differ from the
    # parent; it should not affect the scan-skipping logic.
    run_sql(
        "CREATE TABLE part_6 (c int, LIKE list_parted2, "
        "CONSTRAINT check_a CHECK (a IS NOT NULL AND a = 6))",
        "ALTER TABLE part_6 DROP c",
    )
    output = run_sql(
        "ALTER TABLE list_parted2 ATTACH PARTITION part_6 FOR VALUES IN (6);"
    )
    assert 'verifying table "part_6"' not in output, "table part_6 not scanned"
    assert 'verifying table "list_parted2_def"' in output, "list_parted2_def scanned"
    assert (
        'partition constraint for table "part_6" is implied by existing constraints'
        in output
    )

    # Similar, but the attached table is itself partitioned and its partition
    # has still different attnos for the root partitioning columns.
    run_sql(
        "CREATE TABLE part_7 (LIKE list_parted2, "
        "CONSTRAINT check_a CHECK (a IS NOT NULL AND a = 7)) PARTITION BY LIST (b)",
        "CREATE TABLE part_7_a_null (c int, d int, e int, LIKE list_parted2, "
        "CONSTRAINT check_b CHECK (b IS NULL OR b = 'a'), "
        "CONSTRAINT check_a CHECK (a IS NOT NULL AND a = 7))",
        "ALTER TABLE part_7_a_null DROP c, DROP d, DROP e",
    )

    output = run_sql(
        "ALTER TABLE part_7 ATTACH PARTITION part_7_a_null FOR VALUES IN ('a', null);"
    )
    assert not is_table_verified(output), "table not scanned"
    assert (
        'partition constraint for table "part_7_a_null" is implied by existing '
        "constraints" in output
    )
    output = run_sql(
        "ALTER TABLE list_parted2 ATTACH PARTITION part_7 FOR VALUES IN (7);"
    )
    assert not is_table_verified(output), "tables not scanned"
    assert (
        'partition constraint for table "part_7" is implied by existing constraints'
        in output
    )
    assert (
        'updated partition constraint for default partition "list_parted2_def" is '
        "implied by existing constraints" in output
    )

    run_sql(
        "CREATE TABLE range_parted (a int, b int) PARTITION BY RANGE (a, b)",
        "CREATE TABLE range_part1 (a int NOT NULL CHECK (a = 1), b int NOT NULL)",
    )

    output = run_sql(
        "ALTER TABLE range_parted ATTACH PARTITION range_part1 "
        "FOR VALUES FROM (1, 1) TO (1, 10);"
    )
    assert is_table_verified(output), "table range_part1 scanned"
    assert (
        'partition constraint for table "range_part1" is implied by existing '
        "constraints" not in output
    )

    run_sql(
        "CREATE TABLE range_part2 (a int NOT NULL CHECK (a = 1), "
        "b int NOT NULL CHECK (b >= 10 and b < 18));"
    )
    output = run_sql(
        "ALTER TABLE range_parted ATTACH PARTITION range_part2 "
        "FOR VALUES FROM (1, 10) TO (1, 20);"
    )
    assert not is_table_verified(output), "table range_part2 not scanned"
    assert (
        'partition constraint for table "range_part2" is implied by existing '
        "constraints" in output
    )

    # If a partitioned table being created or attached lacks a constraint that
    # would let the scan be skipped, but an individual partition has one, then
    # the partition's validation scan is skipped.
    run_sql(
        "CREATE TABLE quuux (a int, b text) PARTITION BY LIST (a)",
        "CREATE TABLE quuux_default PARTITION OF quuux DEFAULT PARTITION BY LIST (b)",
        "CREATE TABLE quuux_default1 PARTITION OF quuux_default ("
        "CONSTRAINT check_1 CHECK (a IS NOT NULL AND a = 1)) FOR VALUES IN ('b')",
        "CREATE TABLE quuux1 (a int, b text)",
    )

    output = run_sql("ALTER TABLE quuux ATTACH PARTITION quuux1 FOR VALUES IN (1);")
    assert is_table_verified(output), "quuux1 table scanned"
    assert (
        'partition constraint for table "quuux1" is implied by existing constraints'
        not in output
    )

    run_sql("CREATE TABLE quuux2 (a int, b text);")
    output = run_sql("ALTER TABLE quuux ATTACH PARTITION quuux2 FOR VALUES IN (2);")
    assert 'verifying table "quuux_default1"' not in output, (
        "quuux_default1 not scanned"
    )
    assert 'verifying table "quuux2"' in output, "quuux2 scanned"
    assert (
        'updated partition constraint for default partition "quuux_default1" is '
        "implied by existing constraints" in output
    )
    run_sql("DROP TABLE quuux1, quuux2;")

    # Should validate for quuux1, but not for quuux2.
    output = run_sql("CREATE TABLE quuux1 PARTITION OF quuux FOR VALUES IN (1);")
    assert not is_table_verified(output), "tables not scanned"
    assert (
        'partition constraint for table "quuux1" is implied by existing constraints'
        not in output
    )
    output = run_sql("CREATE TABLE quuux2 PARTITION OF quuux FOR VALUES IN (2);")
    assert not is_table_verified(output), "tables not scanned"
    assert (
        'updated partition constraint for default partition "quuux_default1" is '
        "implied by existing constraints" in output
    )
    run_sql("DROP TABLE quuux;")
