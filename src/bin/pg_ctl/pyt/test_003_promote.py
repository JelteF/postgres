# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/pg_ctl/t/003_promote.pl."""

from pypg.bins import pg_ctl


def test_promote_nonexistent_directory(tmp_path):
    pg_ctl.check_all(
        "--pgdata",
        tmp_path / "nonexistent",
        "promote",
        exit_code=1,
        stderr=r"directory .* does not exist",
    )


def test_promote_of_non_standby_fails(create_pg):
    primary = create_pg("promote_primary", allows_streaming=True)

    # Promote of a not-running instance fails: stopping removes the PID file.
    primary.stop()
    pg_ctl.check_all(
        "--pgdata",
        primary.datadir,
        "promote",
        exit_code=1,
        stderr=r"PID file .* does not exist",
    )

    # Promote of a running primary fails, it is not in standby mode.
    primary.start()
    pg_ctl.check_all(
        "--pgdata",
        primary.datadir,
        "promote",
        exit_code=1,
        stderr=r"not in standby mode",
    )


def test_promote_standby_no_wait(create_pg):
    primary = create_pg("promote_nw_primary", allows_streaming=True)
    backup = primary.backup("my_backup")

    standby = create_pg(
        "promote_nw_standby", from_backup=backup, streaming_primary=primary
    )
    assert standby.sql("SELECT pg_is_in_recovery()") is True, "standby is in recovery"

    pg_ctl("--pgdata", standby.datadir, "--no-wait", "promote")
    standby.poll_query_until("SELECT NOT pg_is_in_recovery()")


def test_promote_standby_wait(create_pg):
    primary = create_pg("promote_w_primary", allows_streaming=True)
    backup = primary.backup("my_backup")

    standby = create_pg(
        "promote_w_standby", from_backup=backup, streaming_primary=primary
    )
    assert standby.sql("SELECT pg_is_in_recovery()") is True, "standby is in recovery"

    # The default (waiting) promote returns only once recovery has ended.
    pg_ctl("--pgdata", standby.datadir, "promote")
    assert standby.sql("SELECT pg_is_in_recovery()") is False
