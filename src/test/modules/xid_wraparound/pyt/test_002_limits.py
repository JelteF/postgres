# Copyright (c) 2023-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/xid_wraparound/t/002_limits.pl.

Tests XID wraparound limits: approaching wraparound first produces warnings,
then a hard stop where the system refuses to assign new XIDs until the oldest
databases have been vacuumed and datfrozenxid advanced.
"""

import re
import warnings

import pytest

from libpq import LibpqError, PostgresWarning
from pypg import require_test_extras
from pypg.util import wait_until

pytestmark = require_test_extras("xid_wraparound")


def test_limits(create_pg):
    node = create_pg(
        "wraparound",
        conf={
            "autovacuum_naptime": "1s",
            "log_autovacuum_min_duration": 0,
        },
    )
    node.sql("CREATE EXTENSION xid_wraparound")

    # Disable autovacuum on the table so it runs only to prevent wraparound.
    node.sql_batch(
        "CREATE TABLE wraparoundtest(t text) WITH (autovacuum_enabled = off)",
        "INSERT INTO wraparoundtest VALUES ('start')",
    )

    # A background session holds a transaction open, preventing autovacuum from
    # advancing relfrozenxid and datfrozenxid.
    bg = node.connect()
    bg.sql_batch("BEGIN", "INSERT INTO wraparoundtest VALUES ('oldxact')")

    # Consume 2 billion transactions, to get close to wraparound.
    node.sql("SELECT consume_xids(1000000000)")
    node.sql("INSERT INTO wraparoundtest VALUES ('after 1 billion')")
    node.sql("SELECT consume_xids(1000000000)")
    node.sql("INSERT INTO wraparoundtest VALUES ('after 2 billion')")

    # Now just under 150 million XIDs from wraparound. Continue consuming XIDs
    # in batches of 10 million until we get the "must be vacuumed within N
    # transactions" warning (surfaced by the notice receiver as a warning).
    warn_limit = False
    for _ in range(15):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            node.sql("SELECT consume_xids(10000000)")
        msgs = "\n".join(
            str(w.message) for w in caught if issubclass(w.category, PostgresWarning)
        )
        if re.search(
            r'database "postgres" must be vacuumed within [0-9]+ transactions', msgs
        ):
            warn_limit = True
            break
    assert warn_limit, "warn-limit reached"

    # We can still INSERT, despite the warnings.
    node.sql("INSERT INTO wraparoundtest VALUES ('reached warn-limit')")

    # Keep going to hit the hard "stop" limit.
    with pytest.raises(
        LibpqError,
        match="database is not accepting commands that assign new transaction IDs "
        'to avoid wraparound data loss in database "postgres"',
    ):
        node.sql("SELECT consume_xids(100000000)")

    # Finish the old transaction so vacuum freezing can advance datfrozenxid.
    bg.sql("COMMIT")
    bg.close()

    # VACUUM to freeze the tables and advance datfrozenxid. Autovacuum does this
    # for the other databases; test manual VACUUM here.
    node.sql("VACUUM")

    # Wait until autovacuum has processed the other databases and advanced the
    # system-wide oldest XID, at which point INSERTs are accepted again.
    for _ in wait_until("INSERTs accepted again", timeout=180):
        try:
            node.sql("INSERT INTO wraparoundtest VALUES ('after VACUUM')")
            break
        except LibpqError:
            pass

    assert node.sql("SELECT * from wraparoundtest") == [
        "start",
        "oldxact",
        "after 1 billion",
        "after 2 billion",
        "reached warn-limit",
        "after VACUUM",
    ]
