# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/pg_verifybackup/t/001_basic.pl."""

from pypg.bins import pg_verifybackup


def test_standard_options():
    pg_verifybackup.check_standard_options()


def test_argument_handling(tmp_path):
    tempdir = str(tmp_path)
    pg_verifybackup.check_all(exit_code=1, stderr=[r"no backup directory specified"])
    pg_verifybackup.check_all(
        tempdir, exit_code=1, stderr=[r'could not open file.*/backup_manifest"']
    )
    pg_verifybackup.check_all(
        tempdir, tempdir, exit_code=1, stderr=[r"too many command-line arguments"]
    )

    # Create a fake manifest file, then point at a different, nonexistent one.
    (tmp_path / "backup_manifest").touch()
    pg_verifybackup.check_all(
        "--manifest-path",
        str(tmp_path / "not_the_manifest"),
        tempdir,
        exit_code=1,
        stderr=[r'could not open file.*/not_the_manifest"'],
    )
