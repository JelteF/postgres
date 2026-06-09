# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/test_misc/t/008_replslot_single_user.pl.

Manipulate replication slots in single-user mode.
"""

import platform

import pytest

# Single-user mode fails on Windows with privileged accounts.
pytestmark = pytest.mark.skipif(
    platform.system() == "Windows", reason="not supported on Windows"
)

SLOT_LOGICAL = "slot_logical"
SLOT_PHYSICAL = "slot_physical"


def test_replslot_single_user(create_pg, pg_bin):
    node = create_pg("replslot_single")
    datadir = str(node.datadir)

    node.sql("CREATE TABLE foo (id int)")
    node.stop()
    node.append_conf("wal_level = logical")

    def single(queries, testname):
        r = pg_bin.run(
            "postgres", "--single", "-F",
            "-c", "exit_on_error=true",
            "-D", datadir, "postgres",
            input=queries,
        )
        assert r.returncode == 0, f"{testname}: {r.stderr}"

    single(
        f"SELECT pg_create_logical_replication_slot('{SLOT_LOGICAL}', 'test_decoding')",
        "logical slot creation",
    )
    single(
        f"SELECT pg_create_physical_replication_slot('{SLOT_PHYSICAL}', true)",
        "physical slot creation",
    )
    single(
        "SELECT pg_create_physical_replication_slot('slot_tmp', true, true)",
        "temporary physical slot creation",
    )
    single(
        "INSERT INTO foo VALUES (1);\n"
        f"SELECT pg_logical_slot_get_changes('{SLOT_LOGICAL}', NULL, NULL);\n",
        "logical decoding",
    )
    single(
        f"SELECT pg_replication_slot_advance('{SLOT_LOGICAL}', pg_current_wal_lsn())",
        "logical slot advance",
    )
    single(
        f"SELECT pg_replication_slot_advance('{SLOT_PHYSICAL}', pg_current_wal_lsn())",
        "physical slot advance",
    )
    single(
        f"SELECT pg_copy_logical_replication_slot('{SLOT_LOGICAL}', 'slot_log_copy')",
        "logical slot copy",
    )
    single(
        f"SELECT pg_copy_physical_replication_slot('{SLOT_PHYSICAL}', 'slot_phy_copy')",
        "physical slot copy",
    )
    single(
        f"SELECT pg_drop_replication_slot('{SLOT_LOGICAL}')",
        "logical slot drop",
    )
    single(
        f"SELECT pg_drop_replication_slot('{SLOT_PHYSICAL}')",
        "physical slot drop",
    )
