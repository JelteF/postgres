# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/001_rep_changes.pl.

Basic end-to-end logical replication test: initial copy and incremental
INSERT/UPDATE/DELETE across a variety of tables (mixed column order, included
index, REPLICA IDENTITY FULL/NOTHING/none, no columns, toasted and dropped
columns), ALTER PUBLICATION ADD/DROP TABLE and publish options, multiple
publications, conflict logging (update_missing/delete_missing), empty-
transaction skipping, GUCs passed through the subscription CONNECTION string
(log_statement_stats -> QUERY STATISTICS in the walsender log), worker restart
on CONNECTION/PUBLICATION/RENAME changes, full cleanup of slots/origins, and
the WARNING from CREATE PUBLICATION under wal_level=minimal.
"""

import re
from decimal import Decimal

from libpq import PostgresWarning

import pytest


def test_rep_changes(create_pg):
    publisher = create_pg("publisher", allows_streaming="logical")
    subscriber = create_pg("subscriber")

    # --- preexisting content on the publisher --------------------------------
    publisher.sql(
        "CREATE FUNCTION public.pg_get_replica_identity_index(int) "
        "RETURNS regclass LANGUAGE sql AS 'SELECT 1/0'"  # shall not be called
    )
    publisher.sql("CREATE TABLE tab_notrep AS SELECT generate_series(1,10) AS a")
    publisher.sql("CREATE TABLE tab_ins AS SELECT generate_series(1,1002) AS a")
    publisher.sql("CREATE TABLE tab_full AS SELECT generate_series(1,10) AS a")
    publisher.sql("CREATE TABLE tab_full2 (x text)")
    publisher.sql("INSERT INTO tab_full2 VALUES ('a'), ('b'), ('b')")
    publisher.sql("CREATE TABLE tab_rep (a int primary key)")
    publisher.sql("CREATE TABLE tab_mixed (a int primary key, b text, c numeric)")
    publisher.sql("INSERT INTO tab_mixed (a, b, c) VALUES (1, 'foo', 1.1)")
    publisher.sql(
        "CREATE TABLE tab_include (a int, b text, "
        "CONSTRAINT covering PRIMARY KEY(a) INCLUDE(b))"
    )
    publisher.sql("CREATE TABLE tab_full_pk (a int primary key, b text)")
    publisher.sql("ALTER TABLE tab_full_pk REPLICA IDENTITY FULL")
    # REPLICA IDENTITY NOTHING allows only INSERT changes.
    publisher.sql("CREATE TABLE tab_nothing (a int)")
    publisher.sql("ALTER TABLE tab_nothing REPLICA IDENTITY NOTHING")
    # Replicate changes without a replica identity index.
    publisher.sql("CREATE TABLE tab_no_replidentity_index(c1 int)")
    publisher.sql(
        "CREATE INDEX idx_no_replidentity_index ON tab_no_replidentity_index(c1)"
    )
    # Replicate changes without columns.
    publisher.sql("CREATE TABLE tab_no_col()")
    publisher.sql("INSERT INTO tab_no_col default VALUES")

    # --- structure on the subscriber -----------------------------------------
    subscriber.sql("CREATE TABLE tab_notrep (a int)")
    subscriber.sql("CREATE TABLE tab_ins (a int)")
    subscriber.sql("CREATE TABLE tab_full (a int)")
    subscriber.sql("CREATE TABLE tab_full2 (x text)")
    subscriber.sql("CREATE TABLE tab_rep (a int primary key)")
    subscriber.sql("CREATE TABLE tab_full_pk (a int primary key, b text)")
    subscriber.sql("ALTER TABLE tab_full_pk REPLICA IDENTITY FULL")
    subscriber.sql("CREATE TABLE tab_nothing (a int)")
    # Different column count and order than on the publisher.
    subscriber.sql(
        "CREATE TABLE tab_mixed (d text default 'local', c numeric, b text, a int primary key)"
    )
    subscriber.sql(
        "CREATE TABLE tab_include (a int, b text, "
        "CONSTRAINT covering PRIMARY KEY(a) INCLUDE(b))"
    )
    subscriber.sql("CREATE TABLE tab_no_replidentity_index(c1 int)")
    subscriber.sql(
        "CREATE INDEX idx_no_replidentity_index ON tab_no_replidentity_index(c1)"
    )
    subscriber.sql("CREATE TABLE tab_no_col()")

    # --- set up logical replication ------------------------------------------
    connstr = publisher.connstr()
    publisher.sql("CREATE PUBLICATION tap_pub")
    publisher.sql("CREATE PUBLICATION tap_pub_ins_only WITH (publish = insert)")
    publisher.sql(
        "ALTER PUBLICATION tap_pub ADD TABLE tab_rep, tab_full, tab_full2, tab_mixed, "
        "tab_include, tab_nothing, tab_full_pk, tab_no_replidentity_index, tab_no_col"
    )
    publisher.sql("ALTER PUBLICATION tap_pub_ins_only ADD TABLE tab_ins")
    subscriber.sql(
        f"CREATE SUBSCRIPTION tap_sub CONNECTION '{connstr}' "
        "PUBLICATION tap_pub, tap_pub_ins_only"
    )
    subscriber.wait_for_subscription_sync(publisher, "tap_sub")

    # Reset IO statistics for the later WAL-sender pg_stat_io check.
    publisher.sql("SELECT pg_stat_reset_shared('io')")

    assert subscriber.sql("SELECT count(*) FROM tab_notrep") == 0, (
        "check non-replicated table is empty on subscriber"
    )
    assert subscriber.sql("SELECT count(*) FROM tab_ins") == 1002, (
        "check initial data was copied to subscriber"
    )

    publisher.sql("INSERT INTO tab_ins SELECT generate_series(1,50)")
    publisher.sql("DELETE FROM tab_ins WHERE a > 20")
    publisher.sql("UPDATE tab_ins SET a = -a")
    publisher.sql("INSERT INTO tab_rep SELECT generate_series(1,50)")
    publisher.sql("DELETE FROM tab_rep WHERE a > 20")
    publisher.sql("UPDATE tab_rep SET a = -a")
    publisher.sql("INSERT INTO tab_mixed VALUES (2, 'bar', 2.2)")
    publisher.sql("INSERT INTO tab_full_pk VALUES (1, 'foo'), (2, 'baz')")
    publisher.sql("INSERT INTO tab_nothing VALUES (generate_series(1,20))")
    publisher.sql("INSERT INTO tab_include SELECT generate_series(1,50)")
    publisher.sql("DELETE FROM tab_include WHERE a > 20")
    publisher.sql("UPDATE tab_include SET a = -a")
    publisher.sql("INSERT INTO tab_no_replidentity_index VALUES(1)")
    publisher.sql("INSERT INTO tab_no_col default VALUES")
    publisher.wait_for_catchup("tap_sub")

    assert subscriber.sql("SELECT count(*), min(a), max(a) FROM tab_ins") == (
        1052,
        1,
        1002,
    ), "check replicated inserts on subscriber"
    assert subscriber.sql("SELECT count(*), min(a), max(a) FROM tab_rep") == (
        20,
        -20,
        -1,
    ), "check replicated changes on subscriber"
    assert subscriber.sql("SELECT * FROM tab_mixed") == [
        ("local", Decimal("1.1"), "foo", 1),
        ("local", Decimal("2.2"), "bar", 2),
    ], "check replicated changes with different column order"
    assert subscriber.sql("SELECT count(*) FROM tab_nothing") == 20, (
        "check replicated changes with REPLICA IDENTITY NOTHING"
    )
    assert subscriber.sql("SELECT count(*), min(a), max(a) FROM tab_include") == (
        20,
        -20,
        -1,
    ), "check replicated changes with primary key index with included columns"
    assert subscriber.sql("SELECT c1 FROM tab_no_replidentity_index") == 1, (
        "value replicated to subscriber without replica identity index"
    )
    assert subscriber.sql("SELECT count(*) FROM tab_no_col") == 2, (
        "check replicated changes for table having no columns"
    )

    # Wait for the logical WAL sender to update its IO statistics.
    publisher.poll_query_until(
        "SELECT sum(reads) > 0 FROM pg_catalog.pg_stat_io "
        "WHERE backend_type = 'walsender' AND object = 'wal'"
    )

    # insert some duplicate rows
    publisher.sql("INSERT INTO tab_full SELECT generate_series(1,10)")

    # --- ALTER PUBLICATION ... DROP TABLE stops sending its changes ----------
    assert subscriber.sql("SELECT count(*), min(a), max(a) FROM tab_ins") == (
        1052,
        1,
        1002,
    ), "check rows on subscriber before table drop from publication"
    publisher.sql("ALTER PUBLICATION tap_pub_ins_only DROP TABLE tab_ins")
    publisher.sql("INSERT INTO tab_ins VALUES(8888)")
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT count(*), min(a), max(a) FROM tab_ins") == (
        1052,
        1,
        1002,
    ), "check rows on subscriber after table drop from publication"
    publisher.sql("DELETE FROM tab_ins WHERE a = 8888")
    publisher.sql("ALTER PUBLICATION tap_pub_ins_only ADD TABLE tab_ins")
    subscriber.sql("ALTER SUBSCRIPTION tap_sub REFRESH PUBLICATION")

    # --- multiple publications; op on table from the first publication -------
    publisher.sql("CREATE TABLE temp1 (a int)")
    publisher.sql("CREATE TABLE temp2 (a int)")
    subscriber.sql("CREATE TABLE temp1 (a int)")
    subscriber.sql("CREATE TABLE temp2 (a int)")
    publisher.sql(
        "CREATE PUBLICATION tap_pub_temp1 FOR TABLE temp1 WITH (publish = insert)"
    )
    publisher.sql("CREATE PUBLICATION tap_pub_temp2 FOR TABLE temp2")
    subscriber.sql(
        f"CREATE SUBSCRIPTION tap_sub_temp1 CONNECTION '{connstr}' "
        "PUBLICATION tap_pub_temp1, tap_pub_temp2"
    )
    subscriber.wait_for_subscription_sync(publisher, "tap_sub_temp1")
    assert subscriber.sql("SELECT count(*) FROM temp1") == 0, (
        "check initial rows on subscriber with multiple publications"
    )
    publisher.sql("INSERT INTO temp1 VALUES (1)")
    publisher.wait_for_catchup("tap_sub_temp1")
    assert subscriber.sql("SELECT count(*) FROM temp1") == 1, (
        "check rows on subscriber with multiple publications"
    )
    subscriber.sql("DROP SUBSCRIPTION tap_sub_temp1")
    publisher.sql("DROP PUBLICATION tap_pub_temp1")
    publisher.sql("DROP PUBLICATION tap_pub_temp2")
    publisher.sql("DROP TABLE temp1")
    publisher.sql("DROP TABLE temp2")
    subscriber.sql("DROP TABLE temp1")
    subscriber.sql("DROP TABLE temp2")

    # --- updates needing REPLICA IDENTITY FULL -------------------------------
    publisher.sql("ALTER TABLE tab_full REPLICA IDENTITY FULL")
    subscriber.sql("ALTER TABLE tab_full REPLICA IDENTITY FULL")
    publisher.sql("ALTER TABLE tab_full2 REPLICA IDENTITY FULL")
    subscriber.sql("ALTER TABLE tab_full2 REPLICA IDENTITY FULL")
    publisher.sql("ALTER TABLE tab_ins REPLICA IDENTITY FULL")
    subscriber.sql("ALTER TABLE tab_ins REPLICA IDENTITY FULL")

    publisher.sql("UPDATE tab_full SET a = a * a")
    publisher.sql("UPDATE tab_full2 SET x = 'bb' WHERE x = 'b'")
    publisher.sql("UPDATE tab_mixed SET b = 'baz' WHERE a = 1")
    publisher.sql("UPDATE tab_full_pk SET b = 'bar' WHERE a = 1")
    publisher.wait_for_catchup("tap_sub")

    assert subscriber.sql("SELECT count(*), min(a), max(a) FROM tab_full") == (
        20,
        1,
        100,
    ), "update works with REPLICA IDENTITY FULL and duplicate tuples"
    assert subscriber.sql("SELECT x FROM tab_full2 ORDER BY 1") == ["a", "bb", "bb"], (
        "update works with REPLICA IDENTITY FULL and text datums"
    )
    assert subscriber.sql("SELECT * FROM tab_mixed ORDER BY a") == [
        ("local", Decimal("1.1"), "baz", 1),
        ("local", Decimal("2.2"), "bar", 2),
    ], "update works with different column order and subscriber local values"
    assert subscriber.sql("SELECT * FROM tab_full_pk ORDER BY a") == [
        (1, "bar"),
        (2, "baz"),
    ], "update works with REPLICA IDENTITY FULL and a primary key"

    subscriber.sql("DELETE FROM tab_full_pk")
    subscriber.sql("DELETE FROM tab_full WHERE a = 25")

    # Grab log positions after a query, so any prior config reload has taken effect.
    log_location_pub = publisher.current_log_position()
    log_location_sub = subscriber.current_log_position()

    publisher.sql("UPDATE tab_full_pk SET b = 'quux' WHERE a = 1")
    publisher.sql("UPDATE tab_full SET a = a + 1 WHERE a = 25")
    publisher.sql("DELETE FROM tab_full_pk WHERE a = 2")
    publisher.wait_for_catchup("tap_sub")

    logtext = subscriber.log_since(log_location_sub)
    assert re.search(
        r'conflict detected on relation "public.tab_full_pk": conflict=update_missing.*\n'
        r".*DETAIL:.* Could not find the row to be updated: "
        r"remote row \(1, quux\), replica identity \(a\)=\(1\)",
        logtext,
    ), "update target row is missing"
    assert re.search(
        r'conflict detected on relation "public.tab_full": conflict=update_missing.*\n'
        r".*DETAIL:.* Could not find the row to be updated: "
        r"remote row \(26\), replica identity full \(25\)",
        logtext,
    ), "update target row is missing"
    assert re.search(
        r'conflict detected on relation "public.tab_full_pk": conflict=delete_missing.*\n'
        r".*DETAIL:.* Could not find the row to be deleted: replica identity \(a\)=\(2\)",
        logtext,
    ), "delete target row is missing"

    subscriber.append_conf(log_min_messages="warning")
    subscriber.pg_ctl("reload")

    # --- toasted values ------------------------------------------------------
    publisher.sql("UPDATE tab_mixed SET b = repeat('xyzzy', 100000) WHERE a = 2")
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT a, length(b), c, d FROM tab_mixed ORDER BY a") == [
        (1, 3, Decimal("1.1"), "local"),
        (2, 500000, Decimal("2.2"), "local"),
    ], "update transmits large column value"

    publisher.sql("UPDATE tab_mixed SET c = 3.3 WHERE a = 2")
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT a, length(b), c, d FROM tab_mixed ORDER BY a") == [
        (1, 3, Decimal("1.1"), "local"),
        (2, 500000, Decimal("3.3"), "local"),
    ], "update with non-transmitted large column value"

    # --- dropped columns -----------------------------------------------------
    # This update is transmitted before the column goes away.
    publisher.sql("UPDATE tab_mixed SET b = 'bar', c = 2.2 WHERE a = 2")
    publisher.sql("ALTER TABLE tab_mixed DROP COLUMN b")
    publisher.sql("UPDATE tab_mixed SET c = 11.11 WHERE a = 1")
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT * FROM tab_mixed ORDER BY a") == [
        ("local", Decimal("11.11"), "baz", 1),
        ("local", Decimal("2.2"), "bar", 2),
    ], "update works with dropped publisher column"

    subscriber.sql("ALTER TABLE tab_mixed DROP COLUMN d")
    publisher.sql("UPDATE tab_mixed SET c = 22.22 WHERE a = 2")
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT * FROM tab_mixed ORDER BY a") == [
        (Decimal("11.11"), "baz", 1),
        (Decimal("22.22"), "bar", 2),
    ], "update works with dropped subscriber column"

    # --- GUCs passed through the subscription CONNECTION string --------------
    # First confirm QUERY STATISTICS is not present before enabling the GUC.
    assert not re.search("QUERY STATISTICS", publisher.log_since(log_location_pub)), (
        "log_statement_stats has not been enabled yet"
    )
    log_location_pub = publisher.current_log_position()

    # Changing the CONNECTION string restarts the apply worker; we also use it
    # to enable log_statement_stats on the walsender.
    oldpid = publisher.sql(
        "SELECT pid FROM pg_stat_replication "
        "WHERE application_name = 'tap_sub' AND state = 'streaming'"
    )
    subscriber.sql(
        f"ALTER SUBSCRIPTION tap_sub CONNECTION "
        f"'{connstr} options=''-c log_statement_stats=on'''"
    )
    publisher.poll_query_until(
        f"SELECT pid != {oldpid} FROM pg_stat_replication "
        "WHERE application_name = 'tap_sub' AND state = 'streaming'"
    )
    assert re.search("QUERY STATISTICS", publisher.log_since(log_location_pub)), (
        "log_statement_stats in CONNECTION string had effect on publisher's walsender"
    )

    # --- worker restart on PUBLICATION change --------------------------------
    oldpid = publisher.sql(
        "SELECT pid FROM pg_stat_replication "
        "WHERE application_name = 'tap_sub' AND state = 'streaming'"
    )
    subscriber.sql(
        "ALTER SUBSCRIPTION tap_sub SET PUBLICATION tap_pub_ins_only WITH (copy_data = false)"
    )
    publisher.poll_query_until(
        f"SELECT pid != {oldpid} FROM pg_stat_replication "
        "WHERE application_name = 'tap_sub' AND state = 'streaming'"
    )

    publisher.sql("INSERT INTO tab_ins SELECT generate_series(1001,1100)")
    publisher.sql("DELETE FROM tab_rep")

    # Restart the publisher; the subscriber should be streaming after catchup.
    publisher.stop(mode="fast")
    publisher.start()
    publisher.wait_for_catchup("tap_sub")

    assert subscriber.sql("SELECT count(*), min(a), max(a) FROM tab_ins") == (
        1152,
        1,
        1100,
    ), "check replicated inserts after subscription publication change"
    assert subscriber.sql("SELECT count(*), min(a), max(a) FROM tab_rep") == (
        20,
        -20,
        -1,
    ), "check changes skipped after subscription publication change"

    # --- ALTER PUBLICATION (relcache invalidation etc.) ----------------------
    publisher.sql("ALTER PUBLICATION tap_pub_ins_only SET (publish = 'insert, delete')")
    publisher.sql("ALTER PUBLICATION tap_pub_ins_only ADD TABLE tab_full")
    publisher.sql("DELETE FROM tab_ins WHERE a > 0")
    subscriber.sql(
        "ALTER SUBSCRIPTION tap_sub REFRESH PUBLICATION WITH (copy_data = false)"
    )
    publisher.sql("INSERT INTO tab_full VALUES(0)")
    publisher.wait_for_catchup("tap_sub")

    # --- empty transaction optimization (DEBUG1) -----------------------------
    publisher.append_conf(log_min_messages="debug1")
    publisher.pg_ctl("reload")
    log_location_pub = publisher.current_log_position()

    # Use a fresh connection so the just-reloaded log_min_messages is in
    # effect for this statement's backend right away, instead of racing
    # the SIGHUP against the cached connection's existing backend.
    publisher.sql_oneshot("INSERT INTO tab_notrep VALUES (11)")
    publisher.wait_for_catchup("tap_sub")
    assert re.search(
        "skipped replication of an empty transaction with XID",
        publisher.log_since(log_location_pub),
    ), "empty transaction is skipped"
    assert subscriber.sql("SELECT count(*) FROM tab_notrep") == 0, (
        "check non-replicated table is empty on subscriber"
    )

    publisher.append_conf(log_min_messages="warning")
    publisher.pg_ctl("reload")

    # data are now intentionally different on publisher and subscriber
    assert subscriber.sql("SELECT count(*), min(a), max(a) FROM tab_ins") == (
        1052,
        1,
        1002,
    ), "check replicated deletes after alter publication"
    assert subscriber.sql("SELECT count(*), min(a), max(a) FROM tab_full") == (
        19,
        0,
        100,
    ), "check replicated insert after alter publication"

    # --- worker restart on RENAME --------------------------------------------
    oldpid = publisher.sql(
        "SELECT pid FROM pg_stat_replication "
        "WHERE application_name = 'tap_sub' AND state = 'streaming'"
    )
    subscriber.sql("ALTER SUBSCRIPTION tap_sub RENAME TO tap_sub_renamed")
    publisher.poll_query_until(
        f"SELECT pid != {oldpid} FROM pg_stat_replication "
        "WHERE application_name = 'tap_sub_renamed' AND state = 'streaming'"
    )

    # --- cleanup -------------------------------------------------------------
    subscriber.sql("DROP SUBSCRIPTION tap_sub_renamed")
    assert subscriber.sql("SELECT count(*) FROM pg_subscription") == 0, (
        "check subscription was dropped on subscriber"
    )
    assert publisher.sql("SELECT count(*) FROM pg_replication_slots") == 0, (
        "check replication slot was dropped on publisher"
    )
    assert subscriber.sql("SELECT count(*) FROM pg_subscription_rel") == 0, (
        "check subscription relation status was dropped on subscriber"
    )
    assert publisher.sql("SELECT count(*) FROM pg_replication_slots") == 0, (
        "check replication slot was dropped on publisher"
    )
    assert subscriber.sql("SELECT count(*) FROM pg_replication_origin") == 0, (
        "check replication origin was dropped on subscriber"
    )

    subscriber.stop(mode="fast")
    publisher.stop(mode="fast")

    # --- CREATE PUBLICATION under wal_level=minimal warns --------------------
    publisher.append_conf(wal_level="minimal", max_wal_senders=0)
    publisher.start()
    with pytest.warns(
        PostgresWarning,
        match="logical decoding must be enabled to publish logical changes",
    ):
        publisher.sql_batch(
            "BEGIN",
            "CREATE TABLE skip_wal()",
            "CREATE PUBLICATION tap_pub2 FOR TABLE skip_wal",
            "ROLLBACK",
        )
