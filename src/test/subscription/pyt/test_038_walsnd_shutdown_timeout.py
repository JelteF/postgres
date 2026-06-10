# Copyright (c) 2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/038_walsnd_shutdown_timeout.pl.

Checks that wal_sender_shutdown_timeout lets the publisher shut down without
waiting to send all pending data when replication is stalled: when the logical
apply worker is blocked on a lock (with the walsender's output buffer empty and
then full), and when both physical and logical replication are stalled while
slot synchronization runs on a standby whose walreceiver has been SIGSTOPped.
"""

import os
import re
import signal
import time

from pypg._env import test_timeout_default

SHUTDOWN_TIMEOUT_LOG = (
    r"WARNING: .* terminating walsender process due to replication shutdown timeout"
)


def test_walsnd_shutdown_timeout(create_pg):
    _create_pg = create_pg

    def create_pg(name, **kwargs):
        node = _create_pg(name, **kwargs)
        node.set_timeout(test_timeout_default)
        return node

    publisher = create_pg(
        "publisher",
        allows_streaming="logical",
        conf=["wal_sender_timeout = 1h", "wal_sender_shutdown_timeout = 10ms"],
    )
    subscriber = create_pg("subscriber")

    publisher.sql(
        """
        CREATE TABLE test_tab (id int PRIMARY KEY);
        CREATE PUBLICATION test_pub FOR TABLE test_tab;
        """
    )
    subscriber.sql("CREATE TABLE test_tab (id int PRIMARY KEY)")
    subscriber.sql(
        f"CREATE SUBSCRIPTION test_sub CONNECTION '{publisher.connstr()}' "
        "PUBLICATION test_pub WITH (failover = true)"
    )
    subscriber.wait_for_subscription_sync(publisher, "test_sub")

    # A background session on the subscriber will block the apply worker on a lock.
    sub_session = subscriber.background()

    # --- apply worker blocked, walsender buffer empty ------------------------
    # Conflicting transactions on publisher and subscriber block the apply worker.
    sub_session.sql("BEGIN; INSERT INTO test_tab VALUES (0)")
    publisher.sql("INSERT INTO test_tab VALUES (0)")

    log_offset = publisher.current_log_position()
    publisher.stop(mode="fast")
    assert re.search(SHUTDOWN_TIMEOUT_LOG, publisher.log_since(log_offset)), (
        "walsender exits due to wal_sender_shutdown_timeout"
    )

    sub_session.sql("ABORT")
    publisher.start()
    publisher.wait_for_catchup("test_sub")

    # --- apply worker blocked, walsender output buffer full ------------------
    sub_session.sql("BEGIN; LOCK TABLE test_tab IN EXCLUSIVE MODE")
    publisher.sql("INSERT INTO test_tab VALUES (generate_series(1, 20000))")

    # Wait for the walsender's output buffer to fill: when sent_lsn stops
    # advancing between checks, treat the buffer as full.
    last_sent_lsn = publisher.sql(
        "SELECT sent_lsn FROM pg_stat_replication WHERE application_name = 'test_sub'"
    )
    for _ in range(test_timeout_default() * 10):
        time.sleep(0.1)
        cur_sent_lsn = publisher.sql(
            "SELECT sent_lsn FROM pg_stat_replication WHERE application_name = 'test_sub'"
        )
        if cur_sent_lsn is None:
            continue
        diff = publisher.sql(f"SELECT pg_wal_lsn_diff('{cur_sent_lsn}', '{last_sent_lsn}')")
        if diff == 0:
            break
        last_sent_lsn = cur_sent_lsn

    log_offset = publisher.current_log_position()
    publisher.stop(mode="fast")
    assert re.search(SHUTDOWN_TIMEOUT_LOG, publisher.log_since(log_offset)), (
        "walsender with full output buffer exits due to wal_sender_shutdown_timeout"
    )

    sub_session.sql("ABORT")
    publisher.start()

    # --- both physical and logical replication stalled, slot sync on standby -
    # Create a standby with slot synchronization enabled.
    backup = publisher.backup(
        "publisher_backup",
        backup_options=[
            "--create-slot", "--slot", "test_slot",
            "-d", "dbname=postgres", "--write-recovery-conf",
        ],
    )
    publisher.append_conf("synchronized_standby_slots = 'test_slot'")
    publisher.pg_ctl("reload")

    standby = create_pg(
        "standby",
        from_backup=backup,
        conf=["sync_replication_slots = on", "hot_standby_feedback = on"],
    )

    # Block the apply worker on a lock, stalling logical replication.
    publisher.wait_for_catchup("test_sub")
    sub_session.sql("BEGIN; LOCK TABLE test_tab IN EXCLUSIVE MODE")
    publisher.sql("INSERT INTO test_tab VALUES (-1)")

    # SIGSTOP the standby's walreceiver, stalling physical replication.
    standby.poll_query_until("SELECT EXISTS(SELECT 1 FROM pg_stat_wal_receiver)")
    receiverpid = standby.sql("SELECT pid FROM pg_stat_wal_receiver")
    assert isinstance(receiverpid, int), f"have walreceiver pid {receiverpid}"
    os.kill(receiverpid, signal.SIGSTOP)

    log_offset = publisher.current_log_position()
    publisher.sql("INSERT INTO test_tab VALUES (-2)")
    publisher.stop(mode="fast")
    assert re.search(SHUTDOWN_TIMEOUT_LOG, publisher.log_since(log_offset)), (
        "walsender exits due to wal_sender_shutdown_timeout even when both physical "
        "and logical replication are stalled"
    )

    os.kill(receiverpid, signal.SIGCONT)
    sub_session.quit()
