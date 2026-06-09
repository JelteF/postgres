# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/pg_verifybackup/t/001_basic.pl."""


def test_standard_options(pg_bin):
    pg_bin.check_help("pg_verifybackup")
    pg_bin.check_version("pg_verifybackup")
    pg_bin.check_bad_option("pg_verifybackup")


def test_argument_handling(pg_bin, tmp_path):
    tempdir = str(tmp_path)
    pg_bin.check_all("pg_verifybackup", exit_code=1,
                     stderr=[r"no backup directory specified"])
    pg_bin.check_all("pg_verifybackup", tempdir, exit_code=1,
                     stderr=[r'could not open file.*/backup_manifest"'])
    pg_bin.check_all("pg_verifybackup", tempdir, tempdir, exit_code=1,
                     stderr=[r"too many command-line arguments"])

    # Create a fake manifest file, then point at a different, nonexistent one.
    (tmp_path / "backup_manifest").touch()
    pg_bin.check_all("pg_verifybackup",
                     "--manifest-path", str(tmp_path / "not_the_manifest"), tempdir,
                     exit_code=1,
                     stderr=[r'could not open file.*/not_the_manifest"'])
