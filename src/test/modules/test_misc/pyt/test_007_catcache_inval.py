# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/test_misc/t/007_catcache_inval.pl.

Tests recursive catalog cache invalidation: an invalidation that arrives
while a catalog cache entry (a catcache list) is still being built.
"""

import random
import string

import pypg

POINT = "catcache-list-miss-systable-scan-started"

pytestmark = pypg.require_injection_points()


def test_catcache_inval(create_pg):
    node = create_pg("catcache_node")

    # Create a function with a large, incompressible body so it is toasted.
    # That matters: the invalidation is accepted during detoasting of the
    # function body while building the catcache list, which is what triggers
    # the recursion under test. A compressible body would stay inline and the
    # invalidation would instead be processed after the list is built.
    longtext = "".join(random.choices(string.ascii_letters + string.digits, k=10000))
    node.sql("CREATE EXTENSION injection_points")
    node.sql(
        "CREATE FUNCTION foofunc(dummy integer) RETURNS integer AS "
        f"$$ SELECT 1; /* {longtext} */ $$ LANGUAGE SQL"
    )

    session = node.connect()
    waker = node.connect()

    # Attach the injection point locally to the first session so only it
    # pauses while populating the catcache list for "foofunc".
    session.sql_batch(
        "SELECT injection_points_set_local()",
        f"SELECT injection_points_attach('{POINT}', 'wait')",
    )

    # Dispatch a call that pauses at the injection point while building the
    # catcache list for functions named "foofunc".
    paused = session.background_sql("SELECT foofunc(1)")
    node.wait_for_injection_point(POINT)

    # While the first session is building the list, overload the same name.
    # This sends a catcache invalidation that arrives mid-build.
    node.sql(
        "CREATE FUNCTION foofunc() RETURNS integer AS $$ SELECT 123 $$ LANGUAGE SQL"
    )

    # Resume the paused session from another session; the SELECT now finishes.
    waker.sql_batch(
        f"SELECT injection_points_wakeup('{POINT}')",
        f"SELECT injection_points_detach('{POINT}')",
    )
    assert paused.result() == 1

    # The newly created overload is visible to the session.
    assert session.sql("SELECT foofunc()") == 123
