# Copyright (c) 2023-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/xid_wraparound/t/003_wraparounds.pl.

Consume a lot of XIDs, wrapping around a few times.
"""

from pypg import require_test_extras

pytestmark = require_test_extras("xid_wraparound")


def test_wraparounds(create_pg):
    node = create_pg(
        "wraparound",
        conf={
            "autovacuum_naptime": "1s",
            # so it's easier to verify the order of operations
            "autovacuum_max_workers": 1,
            "log_autovacuum_min_duration": 0,
        },
    )
    node.sql("CREATE EXTENSION xid_wraparound")

    # Disable autovacuum on the table so it runs only to prevent wraparound.
    node.sql_batch(
        "CREATE TABLE wraparoundtest(t text) WITH (autovacuum_enabled = off);"
        "INSERT INTO wraparoundtest VALUES ('beginning')"
    )

    # Burn through 10 billion transactions in total, in batches of 100 million.
    # Reuse one connection: opening a fresh one per batch can hit the connect
    # timeout while the server is saturated consuming XIDs / aggressively
    # autovacuuming to prevent wraparound.
    with node.connect() as conn:
        for i in range(1, 101):
            conn.sql("SELECT consume_xids(100000000)")
            conn.sql(f"INSERT INTO wraparoundtest VALUES ('after {i} batches')")

    assert node.sql("SELECT COUNT(*) FROM wraparoundtest") == 101
