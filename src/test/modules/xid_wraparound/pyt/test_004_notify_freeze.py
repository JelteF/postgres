# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/xid_wraparound/t/004_notify_freeze.pl.

Tests freezing XIDs in the async notification queue. This isn't really
wraparound-related, but it depends on the consume_xids() helper from the
xid_wraparound module.
"""

from pypg import require_test_extras
from pypg.bins import vacuumdb
from pypg.util import wait_until

pytestmark = require_test_extras("xid_wraparound")


def test_notify_freeze(create_pg):
    node = create_pg("notify_node")
    node.sql("CREATE EXTENSION xid_wraparound")
    node.sql("ALTER DATABASE template0 WITH ALLOW_CONNECTIONS true")

    # Session 1 listens, then sits idle in a transaction so the notifications
    # below stay queued (a session only receives them once its transaction
    # ends).
    session1 = node.background()
    session1.sql("listen s")
    session1.sql("begin")

    # Send some notifies from other sessions.
    for i in range(1, 11):
        node.sql(f"NOTIFY s, '{i}'")

    # Consume enough XIDs to trigger truncation, and one more with
    # txid_current() to bump up the freeze horizon.
    node.sql("select consume_xids(10000000)")
    node.sql("select txid_current()")

    # Remember datfrozenxid before the freeze so we can check it advances.
    before = node.sql("select min(datfrozenxid::text::bigint) from pg_database")

    # Vacuum freeze all databases.
    vacuumdb("--all", "--freeze", server=node)

    after = node.sql("select min(datfrozenxid::text::bigint) from pg_database")
    assert after > before, "datfrozenxid advanced"

    # Commit session 1 and ensure all notifications are received. This depends
    # on correctly freezing the XIDs in the pending notification entries.
    session1.sql("commit")
    notifs = []
    for _ in wait_until("did not receive all notifications", timeout=60):
        notifs.extend(session1.notifies())
        if len(notifs) >= 10:
            break

    assert len(notifs) == 10, "received all committed notifications"
    for expected, n in enumerate(notifs, start=1):
        assert n.channel == "s"
        assert n.payload == str(expected)
