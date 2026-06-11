# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of contrib/basebackup_to_shell/t/001_basic.pl.

Exercises the basebackup_to_shell module: pg_basebackup with a "shell" backup
target pipes the backup through a configured shell command (here gzip writing
to a file). Checks that the module requires its command GUC, honors the %d
target-detail placeholder, and enforces basebackup_to_shell.required_role.
"""

import os
import re
import sys

import pytest

from pypg.util import run

# pg_basebackup options kept small to keep the test fast. -Xfetch because
# -Xstream is unsupported with a backup target; run as backupuser.
BASEBACKUP = [
    "pg_basebackup",
    "--no-sync",
    "--checkpoint",
    "fast",
    "--username",
    "backupuser",
    "--wal-method",
    "fetch",
]


def _verify_backup(pg_bin, prefix, backup_dir, tar, tmp_path, name):
    assert (backup_dir / f"{prefix}backup_manifest.gz").is_file(), (
        f"{name}: backup_manifest.gz was created"
    )
    assert (backup_dir / f"{prefix}base.tar.gz").is_file(), (
        f"{name}: base.tar.gz was created"
    )

    if not tar:
        pytest.skip("no tar program available")

    gzip = os.environ["GZIP_PROGRAM"]
    run(gzip, "-d", str(backup_dir / f"{prefix}backup_manifest.gz"), check=True)
    run(gzip, "-d", str(backup_dir / f"{prefix}base.tar.gz"), check=True)

    extract = tmp_path / f"extract_{prefix or 'none'}"
    extract.mkdir()
    run(tar, "xf", str(backup_dir / f"{prefix}base.tar"), "-C", str(extract), check=True)

    pg_bin.run(
        "pg_verifybackup",
        "--no-parse-wal",
        "--manifest-path",
        str(backup_dir / f"{prefix}backup_manifest"),
        "--exit-on-error",
        str(extract),
        check=True,
    )


def _shell_command(gzip, backup_path, pattern):
    """Build a basebackup_to_shell.command that gzips a segment to a file.

    Mirrors the Perl test's Windows handling: the value is parsed by the config
    file reader, which processes backslash escapes, so on Windows the gzip path
    uses forward slashes and the destination path's backslashes are doubled to
    survive that parsing.
    """
    if sys.platform == "win32":
        gzip = gzip.replace("\\", "/")
        dest = str(backup_path).replace("\\", "\\\\")
        return f'\'"{gzip}" --fast > "{dest}\\\\{pattern}.gz"\''
    return f'\'"{gzip}" --fast > "{backup_path}/{pattern}.gz"\''


def test_basic(create_pg, pg_bin, tmp_path):
    gzip = os.environ.get("GZIP_PROGRAM")
    if not gzip:
        pytest.skip("gzip not available")
    tar = os.environ.get("TAR")

    node = create_pg("primary", allows_streaming=True)
    node.append_conf("shared_preload_libraries = 'basebackup_to_shell'")
    node.pg_ctl("restart")
    node.sql("CREATE USER backupuser REPLICATION")
    node.sql("CREATE ROLE trustworthy")

    # Can't use the module without setting basebackup_to_shell.command.
    r = pg_bin.run(*BASEBACKUP, "--target", "shell", server=node)
    assert r.returncode != 0 and re.search(
        "shell command for backup is not configured", r.stderr
    ), "fails if basebackup_to_shell.command is not set"

    # Configure the command and reload. The shell command runs as the
    # (privilege-dropped on Windows) backend, which must be able to write the
    # backup file, so put it under the test data tree the CI grants ACLs on
    # rather than pytest's tmp_path under the system temp directory.
    backup_path = node.datadir.parent / "backup"
    backup_path.mkdir()
    node.append_conf(
        f"basebackup_to_shell.command={_shell_command(gzip, backup_path, '%f')}"
    )
    node.pg_ctl("reload")

    # Should work now.
    r = pg_bin.run(*BASEBACKUP, "--target", "shell", server=node)
    assert r.returncode == 0, "backup with no detail: pg_basebackup"
    _verify_backup(pg_bin, "", backup_path, tar, tmp_path, "backup with no detail")

    # Should fail when a detail is provided but the command lacks %d.
    r = pg_bin.run(*BASEBACKUP, "--target", "shell:foo", server=node)
    assert r.returncode != 0 and re.search(
        "a target detail is not permitted because the configured command does not "
        "include %d",
        r.stderr,
    ), "fails if detail provided without %d"

    # Reconfigure to require a detail (%d) and restrict to a role.
    node.append_conf(
        f"basebackup_to_shell.command={_shell_command(gzip, backup_path, '%d.%f')}"
    )
    node.append_conf("basebackup_to_shell.required_role='trustworthy'")
    node.pg_ctl("reload")

    # Should fail without the required role.
    r = pg_bin.run(*BASEBACKUP, "--target", "shell", server=node)
    assert r.returncode != 0 and re.search(
        "permission denied to use basebackup_to_shell", r.stderr
    ), "fails if required_role not granted"

    # With the role granted but no detail, the %d requirement is unmet.
    node.sql("GRANT trustworthy TO backupuser")
    r = pg_bin.run(*BASEBACKUP, "--target", "shell", server=node)
    assert r.returncode != 0 and re.search(
        "a target detail is required because the configured command includes %d",
        r.stderr,
    ), "fails if %d is present and detail not given"

    # Should work with a detail.
    r = pg_bin.run(*BASEBACKUP, "--target", "shell:bar", server=node)
    assert r.returncode == 0, "backup with detail: pg_basebackup"
    _verify_backup(pg_bin, "bar.", backup_path, tar, tmp_path, "backup with detail")
