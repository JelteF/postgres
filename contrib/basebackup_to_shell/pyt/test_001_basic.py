# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of contrib/basebackup_to_shell/t/001_basic.pl.

Exercises the basebackup_to_shell module: pg_basebackup with a "shell" backup
target pipes the backup through a configured shell command (here gzip writing
to a file). Checks that the module requires its command GUC, honors the %d
target-detail placeholder, and enforces basebackup_to_shell.required_role.
"""

import os
import sys

import pytest

from pypg.bins import pg_basebackup, pg_verifybackup
from pypg.util import run, shell_path

# pg_basebackup options kept small to keep the test fast. -Xfetch because
# -Xstream is unsupported with a backup target; run as backupuser.
BASEBACKUP = [
    "--no-sync",
    "--checkpoint",
    "fast",
    "--username",
    "backupuser",
    "--wal-method",
    "fetch",
]


def _verify_backup(prefix, backup_dir, tar, tmp_path, name):
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
    run(
        tar, "xf", str(backup_dir / f"{prefix}base.tar"), "-C", str(extract), check=True
    )

    pg_verifybackup(
        "--no-parse-wal",
        "--manifest-path",
        str(backup_dir / f"{prefix}backup_manifest"),
        "--exit-on-error",
        str(extract),
    )


def _shell_command(gzip, backup_path, pattern):
    """Build a basebackup_to_shell.command that gzips a segment to a file.

    append_conf/adjust_conf quote and escape the value for postgresql.conf;
    shell_path renders the destination for the server's shell (backslashes on
    Windows cmd). cmd happily runs a forward-slash executable path.
    """
    if sys.platform == "win32":
        gzip = gzip.replace("\\", "/")
    return f'"{gzip}" --fast > "{shell_path(backup_path / f"{pattern}.gz")}"'


def test_basic(create_pg, tmp_path):
    gzip = os.environ.get("GZIP_PROGRAM")
    if not gzip:
        pytest.skip("gzip not available")
    tar = os.environ.get("TAR")

    node = create_pg(
        "primary",
        allows_streaming=True,
        conf={"shared_preload_libraries": "basebackup_to_shell"},
    )
    node.sql("CREATE USER backupuser REPLICATION")
    node.sql("CREATE ROLE trustworthy")

    # Can't use the module without setting basebackup_to_shell.command.
    pg_basebackup.check_all(
        *BASEBACKUP,
        "--target",
        "shell",
        exit_code=1,
        stderr="shell command for backup is not configured",
        server=node,
    )

    # Configure the command and reload. The shell command runs as the
    # (privilege-dropped on Windows) backend, which must be able to write the
    # backup file, so put it under the test data tree the CI grants ACLs on
    # rather than pytest's tmp_path under the system temp directory.
    # Not named "backup" because that's the framework's own backup directory
    # inside the node's basedir.
    backup_path = node.datadir.parent / "shell_backup"
    backup_path.mkdir()
    node.adjust_conf(
        **{"basebackup_to_shell.command": _shell_command(gzip, backup_path, "%f")}
    )
    node.pg_ctl("reload")

    # Should work now.
    pg_basebackup(*BASEBACKUP, "--target", "shell", server=node)
    _verify_backup("", backup_path, tar, tmp_path, "backup with no detail")

    # Should fail when a detail is provided but the command lacks %d.
    pg_basebackup.check_all(
        *BASEBACKUP,
        "--target",
        "shell:foo",
        exit_code=1,
        stderr="a target detail is not permitted because the configured command "
        "does not include %d",
        server=node,
    )

    # Reconfigure to require a detail (%d) and restrict to a role.
    node.adjust_conf(
        **{"basebackup_to_shell.command": _shell_command(gzip, backup_path, "%d.%f")}
    )
    node.append_conf(**{"basebackup_to_shell.required_role": "trustworthy"})
    node.pg_ctl("reload")

    # Should fail without the required role.
    pg_basebackup.check_all(
        *BASEBACKUP,
        "--target",
        "shell",
        exit_code=1,
        stderr="permission denied to use basebackup_to_shell",
        server=node,
    )

    # With the role granted but no detail, the %d requirement is unmet.
    node.sql("GRANT trustworthy TO backupuser")
    pg_basebackup.check_all(
        *BASEBACKUP,
        "--target",
        "shell",
        exit_code=1,
        stderr="a target detail is required because the configured command includes %d",
        server=node,
    )

    # Should work with a detail.
    pg_basebackup(*BASEBACKUP, "--target", "shell:bar", server=node)
    _verify_backup("bar.", backup_path, tar, tmp_path, "backup with detail")
