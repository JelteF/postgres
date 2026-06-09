# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/003_recovery_targets.pl.

Tests recovery targets: ``immediate``, XID, timestamp, named restore point and
LSN, that a standby stops with the expected amount of data for each, that
conflicting targets and a target that is never reached are reported as errors,
and that out-of-range recovery_target_timeline / recovery_target_xid values are
rejected.
"""

import subprocess

import pytest

from libpq import LibpqError
from pypg.util import run, wait_until


def test_recovery_targets(create_pg, bindir):
    primary = create_pg("primary", allows_streaming=True, archiving=True, start=False)

    # Bump the transaction ID epoch to stress recovery_target_xid parsing.
    run(bindir / "pg_resetwal", "--epoch", "1", primary.datadir)
    primary.start()
    pconn = primary.connect()

    # Data before the backup, for recovery_target = 'immediate'.
    pconn.sql("CREATE TABLE tab_int AS SELECT generate_series(1,1000) AS a")
    lsn1 = primary.sql("SELECT pg_current_wal_lsn()")

    backup = primary.backup("my_backup")

    # Data for a recovery target XID.
    pconn.sql("INSERT INTO tab_int VALUES (generate_series(1001,2000))")
    lsn2, recovery_txid = primary.sql(
        "SELECT pg_current_wal_lsn(), pg_current_xact_id()"
    )

    # Data for a recovery target timestamp.
    pconn.sql("INSERT INTO tab_int VALUES (generate_series(2001,3000))")
    lsn3 = primary.sql("SELECT pg_current_wal_lsn()")
    recovery_time = primary.sql("SELECT now()")

    # Data for a named recovery target.
    pconn.sql("INSERT INTO tab_int VALUES (generate_series(3001,4000))")
    recovery_name = "my_target"
    lsn4 = primary.sql("SELECT pg_current_wal_lsn()")
    pconn.sql(f"SELECT pg_create_restore_point('{recovery_name}')")

    # Data for a recovery target LSN.
    pconn.sql("INSERT INTO tab_int VALUES (generate_series(4001,5000))")
    recovery_lsn = primary.sql("SELECT pg_current_wal_lsn()")

    pconn.sql("INSERT INTO tab_int VALUES (generate_series(5001,6000))")

    # Force the segment holding all of the above to be archived.
    walfile = primary.sql("SELECT pg_walfile_name(pg_current_wal_lsn())")
    pconn.sql("SELECT pg_switch_wal()")
    primary.poll_query_until(
        f"SELECT '{walfile}' <= last_archived_wal FROM pg_stat_archiver"
    )

    def test_recovery_standby(test_name, node_name, recovery_params, num_rows, until_lsn):
        standby = create_pg(
            node_name, from_backup=backup, restoring=primary, conf=recovery_params
        )
        standby.poll_query_until(
            f"SELECT '{until_lsn}'::pg_lsn <= pg_last_wal_replay_lsn()"
        )
        assert standby.sql("SELECT count(*) FROM tab_int") == num_rows, (
            f"check standby content for {test_name}"
        )
        standby.stop()

    test_recovery_standby(
        "immediate target", "standby_1", ["recovery_target = 'immediate'"], 1000, lsn1
    )
    test_recovery_standby(
        "XID", "standby_2", [f"recovery_target_xid = '{recovery_txid}'"], 2000, lsn2
    )
    test_recovery_standby(
        "time", "standby_3", [f"recovery_target_time = '{recovery_time}'"], 3000, lsn3
    )
    test_recovery_standby(
        "name", "standby_4", [f"recovery_target_name = '{recovery_name}'"], 4000, lsn4
    )
    test_recovery_standby(
        "LSN", "standby_5", [f"recovery_target_lsn = '{recovery_lsn}'"], 5000, recovery_lsn
    )

    # Setting the same parameter twice or unsetting one and setting another is
    # allowed; the last non-empty one wins.
    test_recovery_standby(
        "multiple overriding settings",
        "standby_6",
        [
            f"recovery_target_name = '{recovery_name}'",
            "recovery_target_name = ''",
            f"recovery_target_time = '{recovery_time}'",
        ],
        3000,
        lsn3,
    )

    # Conflicting targets of different kinds are rejected at startup.
    standby_7 = create_pg(
        "standby_7",
        from_backup=backup,
        restoring=primary,
        start=False,
        conf=[
            f"recovery_target_name = '{recovery_name}'",
            f"recovery_target_time = '{recovery_time}'",
        ],
    )
    try:
        standby_7.pg_ctl("start")
    except subprocess.CalledProcessError:
        pass  # expected: invalid recovery startup fails
    assert "multiple recovery targets specified" in standby_7.log_since(0), (
        "multiple conflicting settings"
    )

    # A recovery target that is never reached is a fatal error. Using
    # recovery.signal (not a standby) so recovery actually ends.
    standby_8 = create_pg(
        "standby_8",
        from_backup=backup,
        restoring=primary,
        restoring_standby=False,
        start=False,
        conf=["recovery_target_name = 'does_not_exist'"],
    )
    try:
        standby_8.pg_ctl("start")
    except subprocess.CalledProcessError:
        pass
    pidfile = standby_8.datadir / "postmaster.pid"
    for _ in wait_until("standby_8 did not terminate", timeout=180):
        if not pidfile.exists():
            break
    assert "recovery ended before configured recovery target was reached" in (
        standby_8.log_since(0)
    ), "recovery end before target reached is a fatal error"

    # Out-of-range recovery_target_timeline / recovery_target_xid are rejected.
    # The specific reason is in the error's DETAIL, not the primary message.
    def assert_invalid_guc(query, detail):
        with pytest.raises(LibpqError, match="invalid value for parameter") as exc:
            pconn.sql(query)
        assert detail in (exc.value.detail or "")

    assert_invalid_guc(
        "ALTER SYSTEM SET recovery_target_timeline TO 'bogus'", "is not a valid number"
    )
    assert_invalid_guc(
        "ALTER SYSTEM SET recovery_target_timeline TO '0'",
        "must be between 1 and 4294967295",
    )
    assert_invalid_guc(
        "ALTER SYSTEM SET recovery_target_timeline TO '4294967296'",
        "must be between 1 and 4294967295",
    )

    assert_invalid_guc(
        "ALTER SYSTEM SET recovery_target_xid TO 'bogus'", "is not a valid number"
    )
    assert_invalid_guc(
        "ALTER SYSTEM SET recovery_target_xid TO '-1'", "is not a valid number"
    )
    assert_invalid_guc(
        "ALTER SYSTEM SET recovery_target_xid TO '0'",
        "without epoch must be greater than or equal to 3",
    )
