# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/pg_ctl/t/002_status.pl."""


def test_status_nonexistent_directory(pg_bin, tmp_path):
    r = pg_bin.run("pg_ctl", "status", "--pgdata", str(tmp_path / "nonexistent"))
    assert r.returncode == 4, r.stderr


def test_status_tracks_server_state(create_pg, pg_bin):
    node = create_pg("ctlstatus")
    node.stop()
    assert pg_bin.run("pg_ctl", "status", "--pgdata", str(node.datadir)).returncode == 3

    node.start()
    assert pg_bin.run("pg_ctl", "status", "--pgdata", str(node.datadir)).returncode == 0

    node.stop()
