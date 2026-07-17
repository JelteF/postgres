# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/003_constraints.pl.

Checks constraint behaviour on the subscriber: foreign keys are not enforced
during apply, and a REPLICA trigger fires for replicated INSERT/UPDATE.
"""


def test_constraints(create_pg):
    publisher = create_pg("publisher", allows_streaming="logical")
    subscriber = create_pg("subscriber")

    publisher.sql("CREATE TABLE tab_fk (bid int PRIMARY KEY)")
    publisher.sql(
        "CREATE TABLE tab_fk_ref (id int PRIMARY KEY, junk text, "
        "bid int REFERENCES tab_fk (bid))"
    )
    # Subscriber column order intentionally different.
    subscriber.sql("CREATE TABLE tab_fk (bid int PRIMARY KEY)")
    subscriber.sql(
        "CREATE TABLE tab_fk_ref (id int PRIMARY KEY, "
        "bid int REFERENCES tab_fk (bid), junk text)"
    )

    publisher.sql("CREATE PUBLICATION tap_pub FOR ALL TABLES")
    subscriber.sql(
        f"CREATE SUBSCRIPTION tap_sub CONNECTION '{publisher.connstr()}' "
        "PUBLICATION tap_pub WITH (copy_data = false)"
    )
    publisher.wait_for_catchup("tap_sub")

    publisher.sql("INSERT INTO tab_fk (bid) VALUES (1)")
    # "junk" large enough to force out-of-line storage.
    publisher.sql(
        "INSERT INTO tab_fk_ref (id, bid, junk) VALUES (1, 1, repeat(pi()::text,20000))"
    )
    publisher.wait_for_catchup("tap_sub")

    assert subscriber.sql("SELECT count(*), min(bid), max(bid) FROM tab_fk") == (
        1,
        1,
        1,
    ), "check replicated tab_fk inserts on subscriber"
    assert subscriber.sql("SELECT count(*), min(bid), max(bid) FROM tab_fk_ref") == (
        1,
        1,
        1,
    ), "check replicated tab_fk_ref inserts on subscriber"

    # FK is not enforced on the subscriber.
    publisher.sql("DROP TABLE tab_fk CASCADE")
    publisher.sql("INSERT INTO tab_fk_ref (id, bid) VALUES (2, 2)")
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT count(*), min(bid), max(bid) FROM tab_fk_ref") == (
        2,
        1,
        2,
    ), "check FK ignored on subscriber"

    # A replica trigger that filters DML.
    subscriber.sql_batch(
        """
        CREATE FUNCTION filter_basic_dml_fn() RETURNS TRIGGER AS $$
        BEGIN
            IF (TG_OP = 'INSERT') THEN
                IF (NEW.id < 10) THEN
                    RETURN NEW;
                ELSE
                    RETURN NULL;
                END IF;
            ELSIF (TG_OP = 'UPDATE') THEN
                RETURN NULL;
            ELSE
                RAISE WARNING 'Unknown action';
                RETURN NULL;
            END IF;
        END;
        $$ LANGUAGE plpgsql
        """,
        """
        CREATE TRIGGER filter_basic_dml_trg
            BEFORE INSERT OR UPDATE OF bid ON tab_fk_ref
            FOR EACH ROW EXECUTE PROCEDURE filter_basic_dml_fn()
        """,
        "ALTER TABLE tab_fk_ref ENABLE REPLICA TRIGGER filter_basic_dml_trg",
    )

    # The trigger skips the insert of id >= 10 on the subscriber.
    publisher.sql("INSERT INTO tab_fk_ref (id, bid) VALUES (10, 10)")
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT count(*), min(bid), max(bid) FROM tab_fk_ref") == (
        2,
        1,
        2,
    ), "check replica insert trigger applied on subscriber"

    # The trigger skips the update on the subscriber.
    publisher.sql("UPDATE tab_fk_ref SET bid = 2 WHERE bid = 1")
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT count(*), min(bid), max(bid) FROM tab_fk_ref") == (
        2,
        1,
        2,
    ), "check replica update column trigger applied on subscriber"

    # An update on a column not named by the trigger still fires it, because
    # logical replication ships all columns in an update.
    publisher.sql("UPDATE tab_fk_ref SET id = 6 WHERE id = 1")
    publisher.wait_for_catchup("tap_sub")
    assert subscriber.sql("SELECT count(*), min(id), max(id) FROM tab_fk_ref") == (
        2,
        1,
        2,
    ), "check column trigger applied even on update for other column"
