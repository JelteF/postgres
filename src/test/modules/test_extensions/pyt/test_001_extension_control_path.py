# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/test_extensions/t/001_extension_control_path.pl.

Tests extension_control_path: extensions found via custom control directories
are reported with the right location, the location is hidden from
non-superusers, and $system extensions remain visible.
"""

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
        f"/* {sql} */\n"
        f'\\echo Use "CREATE EXTENSION {name}" to load this file. \\quit\n'
    )


def test_extension_control_path(create_pg, tmp_path):
    ext_dir = tmp_path / "ext1"
    ext_dir2 = tmp_path / "ext2"
    (ext_dir / "extension").mkdir(parents=True)
    (ext_dir2 / "extension").mkdir(parents=True)

    ext_name = "test_custom_ext_paths"
    _create_extension(ext_dir, ext_name)
    _create_extension(ext_dir2, ext_name)

    ext_name2 = "test_custom_ext_paths_using_directory"
    (ext_dir / ext_name2).mkdir()
    _create_extension(ext_dir, ext_name2, directory=ext_name2)

    node = create_pg("ext_control_path")
    control_path = f"$system:{ext_dir}:{ext_dir2}"
    node.append_conf(f"extension_control_path = '{control_path}'")
    node.pg_ctl("restart")

    node.sql("CREATE USER user01")

    assert node.sql("SHOW extension_control_path") == control_path

    node.sql(f"CREATE EXTENSION {ext_name}")
    node.sql(f"CREATE EXTENSION {ext_name2}")

    # Both extensions are reported with their custom control-file location (the
    # first matching directory on the path).
    for name in (ext_name, ext_name2):
        assert (
            node.sql(
                f"SELECT location FROM pg_available_extensions WHERE name = '{name}'"
            )
            == f"{ext_dir}/extension"
        )
        assert (
            node.sql(
                "SELECT location FROM pg_available_extension_versions "
                f"WHERE name = '{name}'"
            )
            == f"{ext_dir}/extension"
        )

    # A non-superuser cannot read the extension location.
    user = node.connect(user="user01")
    assert (
        user.sql(
            "SELECT location FROM pg_available_extensions "
            f"WHERE name = '{ext_name2}'"
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
    assert node.sql(
        "SELECT count(*) > 0 FROM pg_available_extensions WHERE name = 'plpgsql'"
    ) is True
    # ... and report the $system location when the path is empty.
    assert (
        node.sql(
            "SET extension_control_path = ''; "
            "SELECT location FROM pg_available_extensions WHERE name = 'plpgsql'"
        )
        == "$system"
    )

    # A genuinely missing extension still errors.
    with pytest.raises(LibpqError, match='extension "invalid" is not available'):
        node.sql("CREATE EXTENSION invalid")
