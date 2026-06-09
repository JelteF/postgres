# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/test_misc/t/008_replslot_single_user.pl.

Manipulate replication slots in single-user mode.
"""

import platform

import pytest

from pypg.bins import postgres

# Single-user mode fails on Windows with privileged accounts.
pytestmark = pytest.mark.skipif(
    platform.system() == "Windows", reason="not supported on Windows"
)

SLOT_LOGICAL = "slot_logical"
SLOT_PHYSICAL = "slot_physical"


def test_replslot_single_user(create_pg):
    node = create_pg("replslot_single")
    datadir = str(node.datadir)

    node.sql("CREATE TABLE foo (id int)")
    node.stop()
    node.append_conf(wal_level="logical")

    def single(queries):
        # exit_on_error=true makes single-user mode exit nonzero on any error,
        # so the call raises (with the server output in the captured log).
        postgres(
            "--single",
            "-F",
            "-c",
            "exit_on_error=true",
            "-D",
            datadir,
            "postgres",
            input=queries,
            encoding="utf-8",
        )

    single(
        f"SELECT pg_create_logical_replication_slot('{SLOT_LOGICAL}', 'test_decoding')"
    )
    single(f"SELECT pg_create_physical_replication_slot('{SLOT_PHYSICAL}', true)")
    single("SELECT pg_create_physical_replication_slot('slot_tmp', true, true)")
    single(
        "INSERT INTO foo VALUES (1);\n"
        f"SELECT pg_logical_slot_get_changes('{SLOT_LOGICAL}', NULL, NULL);\n"
    )
    single(
        f"SELECT pg_replication_slot_advance('{SLOT_LOGICAL}', pg_current_wal_lsn())"
    )
    single(
        f"SELECT pg_replication_slot_advance('{SLOT_PHYSICAL}', pg_current_wal_lsn())"
    )
    single(
        f"SELECT pg_copy_logical_replication_slot('{SLOT_LOGICAL}', 'slot_log_copy')"
    )
    single(
        f"SELECT pg_copy_physical_replication_slot('{SLOT_PHYSICAL}', 'slot_phy_copy')"
    )
    single(f"SELECT pg_drop_replication_slot('{SLOT_LOGICAL}')")
    single(f"SELECT pg_drop_replication_slot('{SLOT_PHYSICAL}')")
