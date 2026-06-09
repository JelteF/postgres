# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/pg_ctl/t/003_promote.pl."""

import re


def test_promote_nonexistent_directory(pg_bin, tmp_path):
    r = pg_bin.run("pg_ctl", "--pgdata", str(tmp_path / "nonexistent"), "promote")
    assert r.returncode != 0
    assert re.search(r"directory .* does not exist", r.stderr), r.stderr


def test_promote_of_non_standby_fails(create_pg, pg_bin):
    primary = create_pg("promote_primary", allows_streaming=True)

    # Promote of a not-running instance fails: stopping removes the PID file.
    primary.stop()
    r = pg_bin.run("pg_ctl", "--pgdata", str(primary.datadir), "promote")
    assert r.returncode != 0
    assert re.search(r"PID file .* does not exist", r.stderr), r.stderr

    # Promote of a running primary fails, it is not in standby mode.
    primary.start()
    r = pg_bin.run("pg_ctl", "--pgdata", str(primary.datadir), "promote")
    assert r.returncode != 0
    assert re.search(r"not in standby mode", r.stderr), r.stderr


def test_promote_standby_no_wait(create_pg, pg_bin):
    primary = create_pg("promote_nw_primary", allows_streaming=True)
    backup = primary.backup("my_backup")

    standby = create_pg(
        "promote_nw_standby", from_backup=backup, streaming_primary=primary
    )
    assert standby.sql("SELECT pg_is_in_recovery()") is True, "standby is in recovery"

    r = pg_bin.run(
        "pg_ctl", "--pgdata", str(standby.datadir), "--no-wait", "promote"
    )
    assert r.returncode == 0, r.stderr
    standby.poll_query_until("SELECT NOT pg_is_in_recovery()")


def test_promote_standby_wait(create_pg, pg_bin):
    primary = create_pg("promote_w_primary", allows_streaming=True)
    backup = primary.backup("my_backup")

    standby = create_pg(
        "promote_w_standby", from_backup=backup, streaming_primary=primary
    )
    assert standby.sql("SELECT pg_is_in_recovery()") is True, "standby is in recovery"

    # The default (waiting) promote returns only once recovery has ended.
    r = pg_bin.run("pg_ctl", "--pgdata", str(standby.datadir), "promote")
    assert r.returncode == 0, r.stderr
    assert standby.sql("SELECT pg_is_in_recovery()") is False
