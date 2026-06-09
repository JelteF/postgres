# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/test_extensions/t/001_extension_control_path.pl.

Tests extension_control_path: extensions found via custom control directories
are reported with the right location, the location is hidden from
non-superusers, and $system extensions remain visible.
"""

import sys

import pytest

from libpq import LibpqError


def _create_extension(ext_dir, name, directory=None):
    """Write a minimal .control and --1.0.sql for ``name`` under ``ext_dir``."""
    control = ext_dir / "extension" / f"{name}.control"
    lines = [
        "comment = 'Test extension_control_path'",
        "default_version = '1.0'",
        "relocatable = true",
    ]
    if directory is not None:
        lines.append(f"directory = {directory}")
        sql = ext_dir / directory / f"{name}--1.0.sql"
    else:
        sql = ext_dir / "extension" / f"{name}--1.0.sql"
    control.write_text("\n".join(lines) + "\n")
    sql.write_text(
        f'/* {sql} */\n\\echo Use "CREATE EXTENSION {name}" to load this file. \\quit\n'
    )


def test_extension_control_path(create_pg):
    node = create_pg("ext_control_path")

    # The server reads the extension control directories, so they must live
    # under the test data tree the CI grants ACLs on (not pytest's tmp_path
    # under the system temp directory), or the privilege-dropped Windows
    # postmaster reports the extensions as "not available".
    ext_dir = node.datadir.parent / "ext1"
    ext_dir2 = node.datadir.parent / "ext2"
    (ext_dir / "extension").mkdir(parents=True)
    (ext_dir2 / "extension").mkdir(parents=True)

    # extension_control_path uses ';' as its list separator on Windows (':'
    # elsewhere, where it would clash with the drive-letter colon).
    # pg_available_extensions.location is canonicalized to forward slashes.
    sep = ";" if sys.platform == "win32" else ":"
    ext_dir_s = ext_dir.as_posix()

    ext_name = "test_custom_ext_paths"
    _create_extension(ext_dir, ext_name)
    _create_extension(ext_dir2, ext_name)

    ext_name2 = "test_custom_ext_paths_using_directory"
    (ext_dir / ext_name2).mkdir()
    _create_extension(ext_dir, ext_name2, directory=ext_name2)

    control_path = f"$system{sep}{ext_dir}{sep}{ext_dir2}"
    node.append_conf(extension_control_path=control_path)
    node.pg_ctl("restart")

    node.sql("CREATE USER user01")

    assert (
        node.sql("SHOW extension_control_path")
        == f"$system{sep}{ext_dir}{sep}{ext_dir2}"
    )

    node.sql(f"CREATE EXTENSION {ext_name}")
    node.sql(f"CREATE EXTENSION {ext_name2}")

    # Both extensions are reported with their custom control-file location (the
    # first matching directory on the path).
    for name in (ext_name, ext_name2):
        assert (
            node.sql(
                f"SELECT location FROM pg_available_extensions WHERE name = '{name}'"
            )
            == f"{ext_dir_s}/extension"
        )
        assert (
            node.sql(
                "SELECT location FROM pg_available_extension_versions "
                f"WHERE name = '{name}'"
            )
            == f"{ext_dir_s}/extension"
        )

    # A non-superuser cannot read the extension location.
    user = node.connect(user="user01")
    assert (
        user.sql(
            f"SELECT location FROM pg_available_extensions WHERE name = '{ext_name2}'"
        )
        == "<insufficient privilege>"
    )
    assert (
        user.sql(
            "SELECT location FROM pg_available_extension_versions "
            f"WHERE name = '{ext_name2}'"
        )
        == "<insufficient privilege>"
    )
    user.close()

    # $system extensions stay visible alongside a custom control path, ...
    assert (
        node.sql(
            "SELECT count(*) > 0 FROM pg_available_extensions WHERE name = 'plpgsql'"
        )
        is True
    )
    # ... and report the $system location when the path is empty. This SET must
    # not leak to the CREATE EXTENSION below, so it runs in its own batch
    # (a fresh connection).
    assert (
        node.sql_batch_oneshot(
            "SET extension_control_path = ''",
            "SELECT location FROM pg_available_extensions WHERE name = 'plpgsql'",
        )[-1]
        == "$system"
    )

    # A genuinely missing extension still errors.
    with pytest.raises(LibpqError, match='extension "invalid" is not available'):
        node.sql("CREATE EXTENSION invalid")
