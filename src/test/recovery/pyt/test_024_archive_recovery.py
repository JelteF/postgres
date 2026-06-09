# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/024_archive_recovery.pl.

Tests that archive recovery of WAL generated with wal_level=minimal fails: a
node started from a backup that then replays a segment produced while the
primary briefly ran at wal_level=minimal must abort recovery with a FATAL. This
is checked both for a plain archive-recovery node and for a standby.
"""

import re
import subprocess

from pypg.util import wait_until

REPLICA_CONFIG = {
    "wal_level": "replica",
    "archive_mode": "on",
    "max_wal_senders": 10,
    "hot_standby": False,
}


def test_archive_recovery(create_pg):
    primary = create_pg(
        "orig", archiving=True, allows_streaming=True, conf=REPLICA_CONFIG
    )
    backup = primary.backup("my_backup")

    # Restart at wal_level=minimal (archiving off) to generate WAL with that
    # setting; this WAL is not archived yet.
    primary.append_conf(wal_level="minimal", archive_mode="off", max_wal_senders=0)
    primary.pg_ctl("restart")

    # Restart back at wal_level=replica with archiving on, to archive the WAL
    # generated above (including the record changing wal_level to minimal).
    primary.append_conf(**REPLICA_CONFIG)
    primary.pg_ctl("restart")

    walfile = primary.sql("SELECT pg_walfile_name(pg_current_wal_lsn())")
    primary.sql("SELECT pg_switch_wal()")
    primary.poll_query_until(
        f"SELECT '{walfile}' <= last_archived_wal FROM pg_stat_archiver"
    )
    primary.stop()

    def check_recovery_fails(name, restoring_standby):
        # The node is expected to abort during recovery, so start it without
        # waiting for it to become ready, then wait for it to terminate.
        node = create_pg(
            name,
            from_backup=backup,
            restoring=primary,
            restoring_standby=restoring_standby,
            start=False,
        )
        try:
            node.pg_ctl("start")
        except subprocess.CalledProcessError:
            pass  # expected: the server fails to start

        pidfile = node.datadir / "postmaster.pid"
        for _ in wait_until(f"{name} did not terminate", timeout=180):
            if not pidfile.exists():
                break

        assert re.search(
            r'FATAL: .* WAL was generated with "wal_level=minimal", '
            r"cannot continue recovering",
            node.log_since(0),
        ), f"{name} ends with an error because it finds wal_level=minimal WAL"

    check_recovery_fails("archive_recovery", restoring_standby=False)
    check_recovery_fails("standby", restoring_standby=True)
