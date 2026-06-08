# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/test_shmem/t/001_late_shmem_alloc.pl."""


def test_late_shmem_alloc(create_pg):
    node = create_pg("main")

    # Allocate memory after startup, i.e. with the library loaded on demand
    # rather than via shared_preload_libraries.
    node.sql("CREATE EXTENSION test_shmem;")

    # Each call must run in its own backend for the per-backend attach
    # callback to increment the counter, so use sql_oneshot() rather than the
    # cached connection sql() uses.
    attach_count1 = node.sql_oneshot("SELECT get_test_shmem_attach_count();")
    attach_count2 = node.sql_oneshot("SELECT get_test_shmem_attach_count();")
    assert attach_count2 > attach_count1, "attach callback is called in each backend"

    # Loading via shared_preload_libraries instead.
    node.append_conf(shared_preload_libraries="test_shmem")
    node.pg_ctl("restart")

    # When preloaded, whether the attach callback runs per backend depends on
    # whether this is an EXEC_BACKEND build.
    exec_backend = node.sql("SHOW debug_exec_backend;") == "on"
    attach_count1 = node.sql_oneshot("SELECT get_test_shmem_attach_count();")
    attach_count2 = node.sql_oneshot("SELECT get_test_shmem_attach_count();")
    if exec_backend:
        assert attach_count2 > attach_count1, (
            "attach callback is called in each backend when loaded via"
            " shared_preload_libraries"
        )
    else:
        assert attach_count1 == 0 and attach_count2 == 0, (
            "attach callback is not called when loaded via shared_preload_libraries"
        )
