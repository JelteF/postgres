# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/test_slru/t/001_multixact.pl.

Tests a multixid corner case: three multixacts are created, but the middle
one is never WAL-logged or recorded on the offsets page because the backend
is paused at an injection point and the server is crashed before it finishes.
After restart the other two multixacts must still be readable.
"""

import pypg

pytestmark = pypg.require_injection_points()


def test_multixact(create_pg):
    node = create_pg(
        "slru_main",
        conf={"shared_preload_libraries": "test_slru,injection_points"},
    )
    node.sql("CREATE EXTENSION injection_points")
    node.sql("CREATE EXTENSION test_slru")

    # Create the first multixact.
    multi1 = node.sql("SELECT test_create_multixact()")

    # Assign the middle multixact, using an injection point to pause the
    # backend before it is fully recorded.
    node.sql("SELECT injection_points_attach('multixact-create-from-members', 'wait')")
    # Dispatch the assignment; it blocks at the injection point.
    lost_multi = node.background_sql_oneshot("SELECT test_create_multixact()")
    node.wait_for_event("client backend", "multixact-create-from-members")
    node.sql("SELECT injection_points_detach('multixact-create-from-members')")

    # Create the third multixact.
    multi2 = node.sql("SELECT test_create_multixact()")

    # Hard crash while the middle multixact's backend is still paused, so it is
    # lost. The blocked background query dies with the server; its result is
    # intentionally never collected.
    node.stop("immediate")
    assert lost_multi.exception() is not None
    node.start()

    # The first and third multixacts are still readable despite the gap.
    assert node.sql(f"SELECT test_read_multixact('{multi1}')") == ""
    assert node.sql(f"SELECT test_read_multixact('{multi2}')") == ""
