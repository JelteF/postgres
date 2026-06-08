# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/pg_controldata/t/001_pg_controldata.pl."""

from pypg.bins import pg_controldata


def test_standard_options():
    pg_controldata.check_standard_options()


def test_no_arguments_fails():
    pg_controldata.check_all(exit_code=1)


def test_nonexistent_directory_fails():
    pg_controldata.check_all("nonexistent", exit_code=1)


def test_produces_output(create_pg):
    node = create_pg("output")
    node.stop()
    pg_controldata.check_all(node.datadir, exit_code=0, stdout=r"checkpoint")


def test_corrupted_pg_control(create_pg):
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

    r = pg_controldata.check_all(
        node.datadir,
        exit_code=0,
        stderr=[
            "calculated CRC checksum does not match value stored in control file",
            "invalid WAL segment size",
        ],
    )
    assert r.stdout
