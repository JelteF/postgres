# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/100_bugs.pl.

Regression tests for assorted logical-replication bugs found over time:
index-predicate constant-folding crash (#15114); temp/unlogged tables ignored
by FOR ALL TABLES; initial-sync protocol with a pre-created slot (#16643) and
cascaded tablesync; relcache invalidation when REPLICA IDENTITY INDEX changes;
schema-rename invalidation; REPLICA IDENTITY FULL with dropped columns;
pgoutput not filling missing attributes with NULL; create-then-immediately-drop
of a replication slot; replication origin advancing when a PL/pgSQL trigger
swallows an error; and the DROP SUBSCRIPTION self-deadlock (#18988).
"""

from libpq import LibpqError
from pypg._env import test_timeout_default

import pytest


def test_bugs(create_pg):
    # Many sections with sequential waits; give each node a fresh full timeout
    # per poll rather than a single shared per-test deadline.
    _create_pg = create_pg

    def create_pg(name, **kwargs):
        node = _create_pg(name, **kwargs)
        node.set_timeout(test_timeout_default)
        return node

    publisher = create_pg("publisher", allows_streaming="logical")
    subscriber = create_pg("subscriber")
    connstr = publisher.connstr()

    # ===== Bug #15114: index predicate constant-folding crash ===============
    # RelationGetIndexAttrBitmap() used to run eval_const_expressions() on index
    # predicates without a snapshot set, crashing on both publisher and subscriber.
    for node in (publisher, subscriber):
        node.sql("CREATE TABLE tab1 (a int PRIMARY KEY, b int)")
        node.sql(
            "CREATE FUNCTION double(x int) RETURNS int IMMUTABLE LANGUAGE SQL "
            "AS 'select x * 2'"
        )
        node.sql("CREATE INDEX ON tab1 (b) WHERE a > double(1)")
    publisher.sql("CREATE PUBLICATION pub1 FOR ALL TABLES")
    subscriber.sql(f"CREATE SUBSCRIPTION sub1 CONNECTION '{connstr}' PUBLICATION pub1")
    publisher.wait_for_catchup("sub1")
    # This would crash, first on the publisher then on the subscriber.
    publisher.sql("INSERT INTO tab1 VALUES (1, 2)")
    publisher.wait_for_catchup("sub1")  # no crash == pass

    subscriber.sql("DROP SUBSCRIPTION sub1")
    publisher.sql("DROP PUBLICATION pub1")
    publisher.sql("DROP TABLE tab1")

    # ===== temp/unlogged tables ignored by FOR ALL TABLES ===================
    # A FOR ALL TABLES publication must not require a replica identity on temp
    # or unlogged tables (their changes aren't published); the UPDATEs succeed.
    publisher.sql("CREATE PUBLICATION pub FOR ALL TABLES")
    publisher.sql("CREATE TEMPORARY TABLE tt1 AS SELECT 1 AS a; UPDATE tt1 SET a = 2")
    publisher.sql("CREATE UNLOGGED TABLE tu1 AS SELECT 1 AS a; UPDATE tu1 SET a = 2")
    publisher.sql("DROP PUBLICATION pub")

    # ===== Bug #16643: initial sync with a pre-created slot =================
    node_twoways = create_pg("twoways", allows_streaming="logical")
    for db in ("d1", "d2"):
        node_twoways.sql(f"CREATE DATABASE {db}")
        node_twoways.sql("CREATE TABLE t (f int)", dbname=db)
        node_twoways.sql("CREATE TABLE t2 (f int)", dbname=db)

    rows = 3000
    node_twoways.sql(
        f"INSERT INTO t SELECT * FROM generate_series(1, {rows}); "
        f"INSERT INTO t2 SELECT * FROM generate_series(1, {rows}); "
        "CREATE PUBLICATION testpub FOR TABLE t",
        dbname="d1",
    )
    node_twoways.sql(
        "SELECT pg_create_logical_replication_slot('testslot', 'pgoutput')", dbname="d1"
    )
    node_twoways.sql(
        f"CREATE SUBSCRIPTION testsub CONNECTION '{node_twoways.connstr(dbname='d1')}' "
        "PUBLICATION testpub WITH (create_slot=false, slot_name='testslot')",
        dbname="d2",
    )
    node_twoways.sql(
        f"INSERT INTO t SELECT * FROM generate_series(1, {rows}); "
        f"INSERT INTO t2 SELECT * FROM generate_series(1, {rows})",
        dbname="d1",
    )
    node_twoways.sql("ALTER PUBLICATION testpub ADD TABLE t2", dbname="d1")
    node_twoways.sql("ALTER SUBSCRIPTION testsub REFRESH PUBLICATION", dbname="d2")
    # wait_for_catchup alone isn't enough: tablesync workers may still run.
    node_twoways.wait_for_subscription_sync(node_twoways, "testsub", "d2")
    assert node_twoways.sql("SELECT count(f) FROM t", dbname="d2") == rows * 2, f"2x{rows} rows in t"
    assert node_twoways.sql("SELECT count(f) FROM t2", dbname="d2") == rows * 2, f"2x{rows} rows in t2"

    # ===== cascaded tablesync data is replicated onward =====================
    node_pub = create_pg("testpublisher1", allows_streaming="logical")
    node_pub_sub = create_pg("testpublisher_subscriber", allows_streaming="logical")
    node_sub = create_pg("testsubscriber1")
    for node in (node_pub, node_pub_sub, node_sub):
        node.sql("CREATE TABLE tab1 (a int)")
    node_pub.sql("CREATE PUBLICATION testpub1 FOR TABLE tab1")
    node_pub_sub.sql("CREATE PUBLICATION testpub2 FOR TABLE tab1")
    # testsub2 must be created before testsub1 to test that the data written by
    # node_pub_sub's tablesync worker gets replicated onward.
    node_sub.sql(
        f"CREATE SUBSCRIPTION testsub2 CONNECTION '{node_pub_sub.connstr()}' PUBLICATION testpub2"
    )
    node_pub_sub.sql(
        f"CREATE SUBSCRIPTION testsub1 CONNECTION '{node_pub.connstr()}' PUBLICATION testpub1"
    )
    node_pub.sql("INSERT INTO tab1 values(generate_series(1,10))")
    node_pub.wait_for_catchup("testsub1")
    node_pub_sub.wait_for_catchup("testsub2")
    node_pub_sub.sql("DROP SUBSCRIPTION testsub1")
    node_sub.sql("DROP SUBSCRIPTION testsub2")

    # ===== REPLICA IDENTITY INDEX change invalidates relcache ===============
    publisher.sql("CREATE TABLE tab_replidentity_index(a int not null, b int not null)")
    publisher.sql("CREATE UNIQUE INDEX idx_replidentity_index_a ON tab_replidentity_index(a)")
    publisher.sql("CREATE UNIQUE INDEX idx_replidentity_index_b ON tab_replidentity_index(b)")
    publisher.sql(
        "ALTER TABLE tab_replidentity_index REPLICA IDENTITY USING INDEX idx_replidentity_index_a"
    )
    publisher.sql("INSERT INTO tab_replidentity_index VALUES(1, 1),(2, 2)")
    subscriber.sql("CREATE TABLE tab_replidentity_index(a int not null, b int not null)")
    subscriber.sql("CREATE UNIQUE INDEX idx_replidentity_index_a ON tab_replidentity_index(a)")
    subscriber.sql("CREATE UNIQUE INDEX idx_replidentity_index_b ON tab_replidentity_index(b)")
    # Subscriber uses index _b, mirroring the future "change RI index" scenario.
    subscriber.sql(
        "ALTER TABLE tab_replidentity_index REPLICA IDENTITY USING INDEX idx_replidentity_index_b"
    )
    publisher.sql("CREATE PUBLICATION tap_pub FOR TABLE tab_replidentity_index")
    subscriber.sql(f"CREATE SUBSCRIPTION tap_sub CONNECTION '{connstr}' PUBLICATION tap_pub")
    subscriber.wait_for_subscription_sync(publisher, "tap_sub")
    assert subscriber.sql("SELECT * FROM tab_replidentity_index") == [
        (1, 1),
        (2, 2),
    ], "check initial data on subscriber"

    publisher.sql(
        """
        ALTER TABLE tab_replidentity_index REPLICA IDENTITY USING INDEX idx_replidentity_index_b;
        UPDATE tab_replidentity_index SET a = -a WHERE a = 1;
        DELETE FROM tab_replidentity_index WHERE a = 2;
        """
    )
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT * FROM tab_replidentity_index") == (-1, 1), (
        "update works with REPLICA IDENTITY"
    )

    subscriber.sql("DROP SUBSCRIPTION tap_sub")
    publisher.sql("DROP PUBLICATION tap_pub")
    publisher.sql("DROP TABLE tab_replidentity_index")
    subscriber.sql("DROP TABLE tab_replidentity_index")

    # ===== schema invalidation on rename ====================================
    publisher.sql("CREATE SCHEMA sch1")
    publisher.sql("CREATE TABLE sch1.t1 (c1 int)")
    subscriber.sql("CREATE SCHEMA sch1")
    subscriber.sql("CREATE TABLE sch1.t1 (c1 int)")
    subscriber.sql("CREATE SCHEMA sch2")
    subscriber.sql("CREATE TABLE sch2.t1 (c1 int)")
    publisher.sql("CREATE PUBLICATION tap_pub_sch FOR ALL TABLES")
    subscriber.sql(f"CREATE SUBSCRIPTION tap_sub_sch CONNECTION '{connstr}' PUBLICATION tap_pub_sch")
    subscriber.wait_for_subscription_sync(publisher, "tap_sub_sch")

    publisher.sql(
        """
        begin;
        insert into sch1.t1 values(1);
        alter schema sch1 rename to sch2;
        create schema sch1;
        create table sch1.t1(c1 int);
        insert into sch1.t1 values(2);
        insert into sch2.t1 values(3);
        commit;
        """
    )
    subscriber.wait_for_subscription_sync(publisher, "tap_sub_sch")
    # New sch1.t1 gets row 2, not the row 1 inserted into the old (renamed) sch1.t1.
    assert subscriber.sql("SELECT * FROM sch1.t1") == [1, 2], (
        "check data in subscriber sch1.t1 after schema rename"
    )
    # sch2.t1 has nothing yet ...
    assert subscriber.sql("SELECT * FROM sch2.t1") == [], (
        "no data yet in subscriber sch2.t1 after schema rename"
    )
    # ... until a REFRESH.
    subscriber.sql("ALTER SUBSCRIPTION tap_sub_sch REFRESH PUBLICATION")
    subscriber.wait_for_subscription_sync(publisher, "tap_sub_sch")
    assert subscriber.sql("SELECT * FROM sch2.t1") == [1, 3], (
        "check data in subscriber sch2.t1 after schema rename"
    )

    subscriber.sql("DROP SUBSCRIPTION tap_sub_sch")
    publisher.sql("DROP PUBLICATION tap_pub_sch")

    # ===== REPLICA IDENTITY FULL with dropped columns =======================
    publisher.sql(
        """
        CREATE TABLE dropped_cols (a int, b_drop int, c int);
        ALTER TABLE dropped_cols REPLICA IDENTITY FULL;
        CREATE PUBLICATION pub_dropped_cols FOR TABLE dropped_cols;
        INSERT INTO dropped_cols VALUES (1, 1, 1);
        """
    )
    subscriber.sql("CREATE TABLE dropped_cols (a int, b_drop int, c int)")
    subscriber.sql(
        f"CREATE SUBSCRIPTION sub_dropped_cols CONNECTION '{connstr}' PUBLICATION pub_dropped_cols"
    )
    subscriber.wait_for_subscription_sync()
    publisher.sql("ALTER TABLE dropped_cols DROP COLUMN b_drop")
    subscriber.sql("ALTER TABLE dropped_cols DROP COLUMN b_drop")
    publisher.sql("UPDATE dropped_cols SET a = 100")
    publisher.wait_for_catchup("sub_dropped_cols")
    assert subscriber.sql("SELECT count(*) FROM dropped_cols WHERE a = 100") == 1, (
        "replication with RI FULL and dropped columns"
    )
    subscriber.sql("DROP SUBSCRIPTION sub_dropped_cols")
    publisher.sql("DROP PUBLICATION pub_dropped_cols")
    publisher.sql("DROP TABLE dropped_cols")
    subscriber.sql("DROP TABLE dropped_cols")

    # ===== pgoutput must not replace missing attributes with NULL ===========
    # The `b` attribute is missing for the first row (ADD COLUMN ... DEFAULT fast path).
    publisher.sql(
        """
        CREATE TABLE tab_default (a int);
        ALTER TABLE tab_default REPLICA IDENTITY FULL;
        INSERT INTO tab_default VALUES (1);
        ALTER TABLE tab_default ADD COLUMN b bool DEFAULT false NOT NULL;
        INSERT INTO tab_default VALUES (2, true);
        CREATE PUBLICATION pub1 FOR TABLE tab_default;
        """
    )
    subscriber.sql("CREATE TABLE tab_default (a int, b bool)")
    subscriber.sql(f"CREATE SUBSCRIPTION sub1 CONNECTION '{connstr}' PUBLICATION pub1")
    subscriber.wait_for_subscription_sync(publisher, "sub1")
    assert subscriber.sql("SELECT a, b FROM tab_default") == [
        (1, False),
        (2, True),
    ], "check snapshot on subscriber"

    publisher.sql("UPDATE tab_default SET a = a + 1")
    publisher.wait_for_catchup("sub1")
    # With the bug, 1|f would not update to 2|f (publisher would send 1|NULL).
    assert subscriber.sql("SELECT a, b FROM tab_default") == [
        (2, False),
        (3, True),
    ], "check replicated update on subscriber"

    # ===== create then immediately drop a replication slot ==================
    # (exposed a memory-management bug in v18); run via replication commands.
    repl = publisher.connect(replication="database")
    repl.sql("CREATE_REPLICATION_SLOT test_slot LOGICAL pgoutput (SNAPSHOT export)")
    repl.sql("DROP_REPLICATION_SLOT test_slot")
    repl.close()

    subscriber.sql("DROP SUBSCRIPTION sub1")
    publisher.sql("DROP PUBLICATION pub1")
    publisher.sql("DROP TABLE tab_default")
    subscriber.sql("DROP TABLE tab_default")

    # ===== replication origin advances when a trigger swallows an error =====
    publisher.sql(
        """
        CREATE TABLE t1 (a int);
        CREATE PUBLICATION regress_pub FOR TABLE t1;
        """
    )
    subscriber.sql("CREATE TABLE t1 (a int)")
    subscriber.sql(f"CREATE SUBSCRIPTION regress_sub CONNECTION '{connstr}' PUBLICATION regress_pub")
    subscriber.wait_for_subscription_sync(publisher, "regress_sub")

    # An AFTER trigger that raises and catches an exception: the apply worker's
    # error callback fires with an ERROR but processing continues.
    subscriber.sql(
        """
        CREATE FUNCTION handle_exception_trigger()
        RETURNS TRIGGER AS $$
        BEGIN
            BEGIN
                RAISE EXCEPTION 'This is a test exception';
            EXCEPTION
                WHEN OTHERS THEN
                    RETURN NEW;
            END;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER silent_exception_trigger
        AFTER INSERT OR UPDATE ON t1
        FOR EACH ROW
        EXECUTE FUNCTION handle_exception_trigger();
        ALTER TABLE t1 ENABLE ALWAYS TRIGGER silent_exception_trigger;
        """
    )
    remote_lsn = subscriber.sql(
        "SELECT remote_lsn::text FROM pg_replication_origin_status os, pg_subscription s "
        "WHERE os.external_id = 'pg_' || s.oid AND s.subname = 'regress_sub'"
    )
    publisher.sql("INSERT INTO t1 VALUES (1)")
    publisher.wait_for_catchup("regress_sub")
    assert subscriber.sql(
        f"SELECT remote_lsn > '{remote_lsn}' FROM pg_replication_origin_status os, "
        "pg_subscription s WHERE os.external_id = 'pg_' || s.oid AND s.subname = 'regress_sub'"
    ) is True, "remote_lsn has advanced for apply worker raising an exception"

    # ===== Bug #18988: DROP SUBSCRIPTION self-deadlock ======================
    # DROP SUBSCRIPTION used to self-deadlock with the walsender over
    # pg_subscription when removing a slot via a newly created database whose
    # caches were not initialized; fixed by lowering the lock level. Here the
    # slot does not exist (connect=false), so the drop simply errors.
    publisher.sql("CREATE DATABASE regress_db")
    regress_db_connstr = publisher.connstr(dbname="regress_db")
    publisher.sql(
        f"CREATE SUBSCRIPTION regress_sub1 CONNECTION '{regress_db_connstr}' "
        "PUBLICATION regress_pub WITH (connect=false)"
    )
    with pytest.raises(
        LibpqError, match=r'could not drop replication slot "regress_sub1" on publisher'
    ):
        publisher.sql("DROP SUBSCRIPTION regress_sub1")
    publisher.sql("DROP DATABASE regress_db")
