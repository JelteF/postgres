# Copyright (c) 2023-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/xid_wraparound/t/003_wraparounds.pl.

Consume a lot of XIDs, wrapping around a few times.
"""

from pypg import require_test_extras

pytestmark = require_test_extras("xid_wraparound")


def test_wraparounds(create_pg):
    node = create_pg("wraparound")
    node.append_conf(
        "autovacuum_naptime = 1s",
        # so it's easier to verify the order of operations
        "autovacuum_max_workers = 1",
        "log_autovacuum_min_duration = 0",
    )
    node.pg_ctl("restart")
    node.sql("CREATE EXTENSION xid_wraparound")

    # Disable autovacuum on the table so it runs only to prevent wraparound.
    node.sql(
        "CREATE TABLE wraparoundtest(t text) WITH (autovacuum_enabled = off);"
        "INSERT INTO wraparoundtest VALUES ('beginning')"
    )

    # Burn through 10 billion transactions in total, in batches of 100 million.
    for i in range(1, 101):
        node.sql("SELECT consume_xids(100000000)")
        node.sql(f"INSERT INTO wraparoundtest VALUES ('after {i} batches')")

    assert node.sql("SELECT COUNT(*) FROM wraparoundtest") == 101
