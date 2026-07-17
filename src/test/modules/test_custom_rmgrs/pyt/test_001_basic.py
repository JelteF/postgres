# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/test_custom_rmgrs/t/001_basic.pl."""


def test_custom_rmgr(create_pg):
    node = create_pg(
        "custom_rmgr",
        conf={
            "wal_level": "replica",
            "max_wal_senders": 4,
            "shared_preload_libraries": "test_custom_rmgrs",
        },
    )

    node.sql("CREATE EXTENSION test_custom_rmgrs")
    # pg_walinspect is only needed to verify test_custom_rmgrs' output;
    # test_custom_rmgrs itself does not depend on it.
    node.sql("CREATE EXTENSION pg_walinspect")

    # Create a slot so checkpoints don't remove the WAL we want to inspect.
    start_lsn = node.sql(
        "SELECT lsn FROM"
        " pg_create_physical_replication_slot('regress_test_slot1', true, false)"
    )
    record_end_lsn = node.sql(
        "SELECT * FROM test_custom_rmgrs_insert_wal_record('payload123')"
    )
    # Ensure the WAL is written and flushed to disk.
    node.sql("SELECT pg_switch_wal()")
    end_lsn = node.sql("SELECT pg_current_wal_flush_lsn()")

    # The custom WAL resource manager registered with the server.
    assert (
        node.sql(
            "SELECT count(*) FROM pg_get_wal_resource_managers()"
            " WHERE rm_name = 'test_custom_rmgrs'"
        )
        == 1
    )

    # ... and successfully wrote our WAL record.
    assert node.sql(
        "SELECT end_lsn, resource_manager, record_type, fpi_length, description"
        f" FROM pg_get_wal_records_info('{start_lsn}', '{end_lsn}')"
        " WHERE resource_manager = 'test_custom_rmgrs'"
    ) == (
        record_end_lsn,
        "test_custom_rmgrs",
        "TEST_CUSTOM_RMGRS_MESSAGE",
        0,
        "payload (10 bytes): payload123",
    )
