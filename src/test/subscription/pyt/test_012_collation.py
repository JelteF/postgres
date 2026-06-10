# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/012_collation.pl.

Test replication with a nondeterministic ICU collation in the replica identity:
the subscriber must find the row to update using collation-wise (not byte-wise)
equality, for both a replica identity index and replica identity full. Skipped
unless the build supports ICU.
"""

import pytest

from pypg import check_pg_config


def test_collation(create_pg):
    # Gate on the build-time flag rather than the presence of ICU collations in
    # the catalog: a build without ICU support can still report ICU collations
    # (so the catalog check is unreliable), but CREATE COLLATION ... icu errors
    # out with "ICU is not supported in this build".
    if not check_pg_config("#define USE_ICU 1"):
        pytest.skip("ICU not supported by this build")
    publisher = create_pg(
        "publisher",
        allows_streaming="logical",
        initdb_opts=["--locale=C", "--encoding=UTF8"],
    )
    subscriber = create_pg("subscriber", initdb_opts=["--locale=C", "--encoding=UTF8"])

    subscriber.sql(
        "CREATE COLLATION ctest_nondet (provider = icu, locale = 'und', deterministic = false)"
    )

    # Table with a replica identity index. The two rows are collation-wise
    # equal but byte-wise different (different Unicode normal forms).
    publisher.sql("CREATE TABLE tab1 (a text PRIMARY KEY, b text)")
    publisher.sql(r"INSERT INTO tab1 VALUES (U&'\00E4bc', 'foo')")
    subscriber.sql(
        "CREATE TABLE tab1 (a text COLLATE ctest_nondet PRIMARY KEY, b text)"
    )
    subscriber.sql(r"INSERT INTO tab1 VALUES (U&'\0061\0308bc', 'foo')")

    # Table with replica identity full.
    publisher.sql("CREATE TABLE tab2 (a text, b text)")
    publisher.sql("ALTER TABLE tab2 REPLICA IDENTITY FULL")
    publisher.sql(r"INSERT INTO tab2 VALUES (U&'\00E4bc', 'foo')")
    subscriber.sql("CREATE TABLE tab2 (a text COLLATE ctest_nondet, b text)")
    subscriber.sql("ALTER TABLE tab2 REPLICA IDENTITY FULL")
    subscriber.sql(r"INSERT INTO tab2 VALUES (U&'\0061\0308bc', 'foo')")

    publisher.sql("CREATE PUBLICATION pub1 FOR ALL TABLES")
    subscriber.sql(
        f"CREATE SUBSCRIPTION sub1 CONNECTION '{publisher.connstr()}' "
        "PUBLICATION pub1 WITH (copy_data = false)"
    )
    publisher.wait_for_catchup("sub1")

    # Replica identity index: the update doesn't touch the key, so the
    # subscriber must match the row by nondeterministic collation.
    publisher.sql("UPDATE tab1 SET b = 'bar' WHERE b = 'foo'")
    publisher.wait_for_catchup("sub1")
    assert subscriber.sql("SELECT b FROM tab1") == "bar", (
        "update with primary key with nondeterministic collation"
    )

    # Replica identity full.
    publisher.sql("UPDATE tab2 SET b = 'bar' WHERE b = 'foo'")
    publisher.wait_for_catchup("sub1")
    assert subscriber.sql("SELECT b FROM tab2") == "bar", (
        "update with replica identity full with nondeterministic collation"
    )
