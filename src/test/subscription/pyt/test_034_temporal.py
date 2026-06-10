# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/034_temporal.pl.

Logical replication tests for temporal tables (PRIMARY KEY / UNIQUE ... WITHOUT
OVERLAPS, a GiST index). Such an index can serve as REPLICA IDENTITY, so updates
and deletes (including FOR PORTION OF) replicate; a table with no usable replica
identity rejects update/delete on the publisher. Exercised across REPLICA
IDENTITY DEFAULT, FULL, USING INDEX, and NOTHING.
"""

import pytest

from libpq import LibpqError

ROW1 = ("[1,2)", "[2000-01-01,2010-01-01)", "a")
# Result after the standard insert+update(+FOR PORTION OF)+delete sequence on a
# table that has a usable replica identity.
REPLICATED = [
    ("[1,2)", "[2000-01-01,2010-01-01)", "a"),
    ("[2,3)", "[2000-01-01,2001-01-01)", "b"),
    ("[2,3)", "[2001-01-01,2002-01-01)", "c"),
    ("[2,3)", "[2003-01-01,2010-01-01)", "b"),
    ("[4,5)", "[2000-01-01,2010-01-01)", "a"),
]
# Result when update/delete are rejected on the publisher (only inserts apply).
INSERTS_ONLY = [
    ("[1,2)", "[2000-01-01,2010-01-01)", "a"),
    ("[2,3)", "[2000-01-01,2010-01-01)", "a"),
    ("[3,4)", "[2000-01-01,2010-01-01)", "a"),
    ("[4,5)", "[2000-01-01,2010-01-01)", "a"),
]
THREE_MORE = (
    "INSERT INTO {t} (id, valid_at, a) VALUES "
    "('[2,3)', '[2000-01-01,2010-01-01)', 'a'), "
    "('[3,4)', '[2000-01-01,2010-01-01)', 'a'), "
    "('[4,5)', '[2000-01-01,2010-01-01)', 'a')"
)


def test_temporal(create_pg):
    publisher = create_pg("publisher", allows_streaming="logical")
    subscriber = create_pg("subscriber")
    connstr = publisher.connstr()

    def select(table):
        return subscriber.sql(f"SELECT * FROM {table} ORDER BY id, valid_at")

    def cannot(verb, table):
        # "cannot update table ..." vs "cannot delete from table ..."
        action = "update" if verb == "update" else "delete from"
        with pytest.raises(
            LibpqError,
            match=f'cannot {action} table "{table}" because it does not have a replica identity',
        ):
            verb_sql = (
                "UPDATE {t} SET a = 'b'" if verb == "update" else "DELETE FROM {t}"
            )
            publisher.sql(verb_sql.format(t=table) + " WHERE id = '[2,3)'")

    def replicate_modifiable(table):
        publisher.sql(THREE_MORE.format(t=table))
        publisher.sql(f"UPDATE {table} SET a = 'b' WHERE id = '[2,3)'")
        publisher.sql(
            f"UPDATE {table} FOR PORTION OF valid_at FROM '2001-01-01' TO '2002-01-01' "
            "SET a = 'c' WHERE id = '[2,3)'"
        )
        publisher.sql(f"DELETE FROM {table} WHERE id = '[3,4)'")
        publisher.sql(
            f"DELETE FROM {table} FOR PORTION OF valid_at FROM '2002-01-01' TO '2003-01-01' "
            "WHERE id = '[2,3)'"
        )

    def replicate_no_identity(table):
        publisher.sql(THREE_MORE.format(t=table))
        cannot("update", table)
        cannot("delete", table)

    def create_tables(identity=None):
        defs = {
            "temporal_no_key": "id int4range, valid_at daterange, a text",
            "temporal_pk": "id int4range, valid_at daterange, a text, "
            "PRIMARY KEY (id, valid_at WITHOUT OVERLAPS)",
            "temporal_unique": "id int4range, valid_at daterange, a text, "
            "UNIQUE (id, valid_at WITHOUT OVERLAPS)",
        }
        for node in (publisher, subscriber):
            for t, cols in defs.items():
                node.sql(f"CREATE TABLE {t} ({cols})")
                if identity:
                    node.sql(f"ALTER TABLE {t} REPLICA IDENTITY {identity}")

    def setup_pub_sub():
        for t in ("temporal_no_key", "temporal_pk", "temporal_unique"):
            publisher.sql(
                f"INSERT INTO {t} (id, valid_at, a) VALUES "
                "('[1,2)', '[2000-01-01,2010-01-01)', 'a')"
            )
        publisher.sql("CREATE PUBLICATION pub1 FOR ALL TABLES")
        subscriber.sql(
            f"CREATE SUBSCRIPTION sub1 CONNECTION '{connstr}' PUBLICATION pub1"
        )
        subscriber.wait_for_subscription_sync()

    def drop_everything():
        for t in ("temporal_no_key", "temporal_pk", "temporal_unique"):
            publisher.sql(f"DROP TABLE IF EXISTS {t}")
            subscriber.sql(f"DROP TABLE IF EXISTS {t}")
        publisher.sql("DROP PUBLICATION pub1")
        subscriber.sql("DROP SUBSCRIPTION sub1")

    # --- REPLICA IDENTITY DEFAULT ---
    create_tables()
    setup_pub_sub()
    assert select("temporal_no_key") == ROW1, "synced temporal_no_key DEFAULT"
    assert select("temporal_pk") == ROW1, "synced temporal_pk DEFAULT"
    assert select("temporal_unique") == ROW1, "synced temporal_unique DEFAULT"
    replicate_no_identity("temporal_no_key")
    publisher.wait_for_catchup("sub1")
    assert select("temporal_no_key") == INSERTS_ONLY, (
        "replicated temporal_no_key DEFAULT"
    )
    replicate_modifiable("temporal_pk")
    publisher.wait_for_catchup("sub1")
    assert select("temporal_pk") == REPLICATED, "replicated temporal_pk DEFAULT"
    replicate_no_identity("temporal_unique")
    publisher.wait_for_catchup("sub1")
    assert select("temporal_unique") == INSERTS_ONLY, (
        "replicated temporal_unique DEFAULT"
    )
    drop_everything()

    # --- REPLICA IDENTITY FULL (everything replicates) ---
    create_tables("FULL")
    setup_pub_sub()
    for t in ("temporal_no_key", "temporal_pk", "temporal_unique"):
        assert select(t) == ROW1, f"synced {t} FULL"
        replicate_modifiable(t)
        publisher.wait_for_catchup("sub1")
        assert select(t) == REPLICATED, f"replicated {t} FULL"
    drop_everything()

    # --- REPLICA IDENTITY USING INDEX (pk and unique only) ---
    publisher.sql(
        "CREATE TABLE temporal_pk (id int4range, valid_at daterange, a text, "
        "PRIMARY KEY (id, valid_at WITHOUT OVERLAPS))"
    )
    publisher.sql(
        "ALTER TABLE temporal_pk REPLICA IDENTITY USING INDEX temporal_pk_pkey"
    )
    publisher.sql(
        "CREATE TABLE temporal_unique (id int4range NOT NULL, valid_at daterange NOT NULL, "
        "a text, UNIQUE (id, valid_at WITHOUT OVERLAPS))"
    )
    publisher.sql(
        "ALTER TABLE temporal_unique REPLICA IDENTITY USING INDEX temporal_unique_id_valid_at_key"
    )
    subscriber.sql(
        "CREATE TABLE temporal_pk (id int4range, valid_at daterange, a text, "
        "PRIMARY KEY (id, valid_at WITHOUT OVERLAPS))"
    )
    subscriber.sql(
        "ALTER TABLE temporal_pk REPLICA IDENTITY USING INDEX temporal_pk_pkey"
    )
    subscriber.sql(
        "CREATE TABLE temporal_unique (id int4range NOT NULL, valid_at daterange NOT NULL, "
        "a text, UNIQUE (id, valid_at WITHOUT OVERLAPS))"
    )
    subscriber.sql(
        "ALTER TABLE temporal_unique REPLICA IDENTITY USING INDEX temporal_unique_id_valid_at_key"
    )
    for t in ("temporal_pk", "temporal_unique"):
        publisher.sql(
            f"INSERT INTO {t} (id, valid_at, a) VALUES ('[1,2)', '[2000-01-01,2010-01-01)', 'a')"
        )
    publisher.sql("CREATE PUBLICATION pub1 FOR ALL TABLES")
    subscriber.sql(f"CREATE SUBSCRIPTION sub1 CONNECTION '{connstr}' PUBLICATION pub1")
    subscriber.wait_for_subscription_sync()
    for t in ("temporal_pk", "temporal_unique"):
        assert select(t) == ROW1, f"synced {t} USING INDEX"
        replicate_modifiable(t)
        publisher.wait_for_catchup("sub1")
        assert select(t) == REPLICATED, f"replicated {t} USING INDEX"
    for t in ("temporal_pk", "temporal_unique"):
        publisher.sql(f"DROP TABLE IF EXISTS {t}")
        subscriber.sql(f"DROP TABLE IF EXISTS {t}")
    publisher.sql("DROP PUBLICATION pub1")
    subscriber.sql("DROP SUBSCRIPTION sub1")

    # --- REPLICA IDENTITY NOTHING (update/delete rejected on all) ---
    create_tables("NOTHING")
    setup_pub_sub()
    for t in ("temporal_no_key", "temporal_pk", "temporal_unique"):
        assert select(t) == ROW1, f"synced {t} NOTHING"
        replicate_no_identity(t)
        publisher.wait_for_catchup("sub1")
        assert select(t) == INSERTS_ONLY, f"replicated {t} NOTHING"
    drop_everything()
