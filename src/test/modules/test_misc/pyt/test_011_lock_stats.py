# Copyright (c) 2026, PostgreSQL Global Development Group

"""Port of src/test/modules/test_misc/t/011_lock_stats.pl.

Tests lock statistics (pg_stat_lock) and log_lock_waits. A second session
(s2) is made to wait on a lock for longer than deadlock_timeout for several
lock types. It also checks that log_lock_waits messages are emitted both when
the wait occurs and when the lock is acquired, that "still waiting for" is
logged exactly once per wait even if the backend is woken by a signal, and
that log_lock_waits = off does not affect the statistics counters.
"""

import re

from pypg import skip_unless_injection_points

DEADLOCK_TIMEOUT_MS = 10
POINT = "deadlock-timeout-fired"


def test_lock_stats(create_pg):
    node = create_pg("lockstats", conf=[f"deadlock_timeout = {DEADLOCK_TIMEOUT_MS}ms"])
    skip_unless_injection_points(node)
    node.sql("CREATE EXTENSION injection_points")
    node.sql(
        "CREATE TABLE test_stat_tab(key text not null, value int);"
        "INSERT INTO test_stat_tab(key, value) VALUES('k0', 1)"
    )

    def setup_sessions():
        s1 = node.background()
        s2 = node.background()
        # Make the waiting session pause when its deadlock timer fires.
        s2.sql(f"SELECT injection_points_attach('{POINT}', 'wait')")
        return s1, s2

    def wait_and_detach():
        node.wait_for_injection_point(POINT)
        node.sql(
            f"SELECT injection_points_detach('{POINT}');"
            f"SELECT injection_points_wakeup('{POINT}')"
        )

    def wait_for_pg_stat_lock(locktype):
        node.poll_query_until(
            f"SELECT waits > 0 AND wait_time >= {DEADLOCK_TIMEOUT_MS} "
            f"FROM pg_stat_lock WHERE locktype = '{locktype}'"
        )

    # ---- Relation lock ----
    s1, s2 = setup_sessions()
    offset = node.current_log_position()

    s1.sql("SELECT pg_stat_reset_shared('lock'); BEGIN; LOCK TABLE test_stat_tab;")
    s2.sql("BEGIN; SELECT pg_stat_force_next_flush();")
    blocked = s2.asql("LOCK TABLE test_stat_tab")
    wait_and_detach()

    node.wait_for_log(r"still waiting for AccessExclusiveLock on relation", offset)

    # Wake the waiting backend by logging its memory contexts, to later confirm
    # the "still waiting" message is not re-logged on such a wakeup.
    node.sql(
        "SELECT pg_log_backend_memory_contexts(pid) FROM pg_locks "
        "WHERE locktype = 'relation' AND relation = 'test_stat_tab'::regclass "
        "AND NOT granted"
    )
    node.wait_for_log(r"logging memory contexts", offset)

    s1.sql("COMMIT")
    blocked.result()
    s2.sql("COMMIT")

    wait_for_pg_stat_lock("relation")
    node.wait_for_log(r"acquired AccessExclusiveLock on relation", offset)

    still_waiting = re.findall(r"still waiting for", node.log_since(offset))
    assert len(still_waiting) == 1, "still waiting logged exactly once despite wakeups"

    s1.quit()
    s2.quit()

    # ---- Transaction lock ----
    s1, s2 = setup_sessions()
    offset = node.current_log_position()

    s1.sql("SELECT pg_stat_reset_shared('lock')")
    # Commit the rows before opening the transaction, so they are visible to
    # s2 (a single multi-statement string would keep the INSERT in the open
    # transaction and s2's UPDATE would then match no rows and not block).
    s1.sql("INSERT INTO test_stat_tab(key, value) VALUES('k1', 1), ('k2', 1), ('k3', 1)")
    s1.sql("BEGIN")
    s1.sql("UPDATE test_stat_tab SET value = value + 1 WHERE key = 'k1'")
    s2.sql("SET log_lock_waits = on; BEGIN; SELECT pg_stat_force_next_flush();")
    blocked = s2.asql("UPDATE test_stat_tab SET value = value + 1 WHERE key = 'k1'")
    wait_and_detach()

    node.wait_for_log(r"still waiting for ShareLock on transaction", offset)

    s1.sql("COMMIT")
    blocked.result()
    s2.sql("COMMIT")

    wait_for_pg_stat_lock("transactionid")
    node.wait_for_log(r"acquired ShareLock on transaction", offset)

    s1.quit()
    s2.quit()

    # ---- Advisory lock ----
    s1, s2 = setup_sessions()
    offset = node.current_log_position()

    s1.sql("SELECT pg_stat_reset_shared('lock'); SELECT pg_advisory_lock(1);")
    s2.sql("SET log_lock_waits = on; BEGIN; SELECT pg_stat_force_next_flush();")
    blocked = s2.asql("SELECT pg_advisory_lock(1)")
    wait_and_detach()

    node.wait_for_log(r"still waiting for ExclusiveLock on advisory lock", offset)

    s1.sql("SELECT pg_advisory_unlock(1)")
    blocked.result()
    s2.sql("SELECT pg_advisory_unlock(1); COMMIT;")

    wait_for_pg_stat_lock("advisory")
    node.wait_for_log(r"acquired ExclusiveLock on advisory lock", offset)

    s1.quit()
    s2.quit()

    # ---- log_lock_waits = off has no impact on the statistics ----
    s1, s2 = setup_sessions()
    offset = node.current_log_position()

    s1.sql("SELECT pg_stat_reset_shared('lock'); BEGIN; LOCK TABLE test_stat_tab;")
    s2.sql("SET log_lock_waits = off; BEGIN; SELECT pg_stat_force_next_flush();")
    blocked = s2.asql("LOCK TABLE test_stat_tab")
    wait_and_detach()

    s1.sql("COMMIT")
    blocked.result()
    s2.sql("COMMIT")

    wait_for_pg_stat_lock("relation")

    # No log_lock_waits messages should have been emitted.
    log = node.log_since(offset)
    assert "still waiting for AccessExclusiveLock on relation" not in log
    assert "acquired AccessExclusiveLock on relation" not in log

    s1.quit()
    s2.quit()

    node.sql("DROP TABLE test_stat_tab")
