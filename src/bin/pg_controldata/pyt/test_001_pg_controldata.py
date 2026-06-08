# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/pg_controldata/t/001_pg_controldata.pl."""

import re


def test_standard_options(pg_bin):
    pg_bin.check_help("pg_controldata")
    pg_bin.check_version("pg_controldata")
    pg_bin.check_bad_option("pg_controldata")


def test_no_arguments_fails(pg_bin):
    assert pg_bin.run("pg_controldata").returncode != 0


def test_nonexistent_directory_fails(pg_bin):
    assert pg_bin.run("pg_controldata", "nonexistent").returncode != 0


def test_produces_output(create_pg, pg_bin):
    node = create_pg("output")
    node.stop()
    r = pg_bin.run("pg_controldata", node.datadir)
    assert r.returncode == 0
    assert re.search(r"checkpoint", r.stdout)


def test_corrupted_pg_control(create_pg, pg_bin):
    node = create_pg("corrupt")
    node.stop()

    # Overwrite most of pg_control with zeros, leaving the first 16 bytes (the
    # pg_control version number) intact so we get a checksum mismatch rather
    # than a version-number error.
    pg_control = node.datadir / "global" / "pg_control"
    size = pg_control.stat().st_size
    with open(pg_control, "r+b") as f:
        f.seek(16)
        f.write(b"\x00" * (size - 16))

    r = pg_bin.run("pg_controldata", node.datadir)
    assert r.returncode == 0
    assert r.stdout
    assert (
        "calculated CRC checksum does not match value stored in control file"
        in r.stderr
    )
    assert "invalid WAL segment size" in r.stderr
