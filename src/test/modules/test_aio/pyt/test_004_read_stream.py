# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/test_aio/t/004_read_stream.pl.

Exercises read streams over the test_aio extension, once per supported
io_method, including a read stream encountering buffers undergoing IO in
another backend (foreign IO).
"""

import re

import pytest

from libpq import LibpqError
from pypg import skip_unless_injection_points

IO_METHODS = ["worker", "io_uring", "sync"]

CONFIGURE = [
    "shared_preload_libraries = test_aio",
    "log_min_messages = 'DEBUG3'",
    "log_statement = all",
    "log_error_verbosity = default",
    "restart_after_crash = false",
    "temp_buffers = 100",
]


def _supported_io_methods(pg_bin):
    r = pg_bin.run("postgres", "-C", "invalid", "-c", "io_method=invalid")
    m = re.search(r"Available values: ([^.]+)\.", r.stderr)
    assert m, f"can't determine supported io_method values: {r.stderr}"
    return m.group(1)


def wait_block(node, bg, sql, wait_event, current_session=True):
    """Dispatch ``sql`` on background session ``bg`` and wait until it parks on
    ``wait_event``, returning the Future."""
    if current_session:
        pid = bg.sql("SELECT pg_backend_pid()")
        fut = bg.asql(sql)
        node.poll_query_until(
            f"SELECT wait_event FROM pg_stat_activity WHERE pid = {pid}", wait_event
        )
    else:
        fut = bg.asql(sql)
        node.poll_query_until(
            "SELECT count(*) > 0 FROM pg_stat_activity "
            f"WHERE wait_event = '{wait_event}'",
            True,
        )
    return fut


@pytest.fixture(scope="module", params=IO_METHODS)
def aio_node(request, create_pg_module, pg_bin):
    method = request.param
    if method not in _supported_io_methods(pg_bin):
        pytest.skip(f"io_method {method} not supported by this build")

    node = create_pg_module(f"rs_{method}")
    # The Perl uses max_connections=8 to keep resource use low; allow more
    # headroom here because poll_query_until() holds a connection open until
    # teardown, and the foreign-IO tests poll repeatedly.
    node.append_conf(*CONFIGURE, "max_connections = 20", f"io_method = {method}")
    node.pg_ctl("restart")

    assert node.sql("SHOW io_method") == method
    node.sql("CREATE EXTENSION test_aio")
    node.sql("CREATE TABLE largeish(k int not null) WITH (FILLFACTOR=10)")
    node.sql("INSERT INTO largeish(k) SELECT generate_series(1, 10000)")
    return node


def test_repeated_blocks(aio_node):
    """Read streams over repeated misses and hits of the same block."""
    conn = aio_node.connect()
    conn.sql("SET io_combine_limit = 1")  # smaller reads make this easier to test

    conn.sql("SELECT evict_rel('largeish')")
    # block 0 grows the lookahead distance enough that the stream starts a
    # pending read for blocks 2 and 4 twice before returning any buffers.
    conn.sql("SELECT * FROM read_stream_for_blocks('largeish', ARRAY[0, 2, 2, 4, 4])")
    conn.sql("SELECT * FROM read_stream_for_blocks('largeish', ARRAY[0, 2, 2, 4, 4])")

    conn.sql("SELECT evict_rel('largeish')")
    conn.sql(
        "SELECT * FROM read_stream_for_blocks('largeish', "
        "ARRAY[0, 1, 2, 3, 4, 5, 6, 5, 4, 3, 2, 1, 0])"
    )

    # Same with a temp table, evicting individual local buffers.
    conn.sql(
        "CREATE TEMP TABLE largeish_temp(k int not null) WITH (FILLFACTOR=10)"
    )
    conn.sql("INSERT INTO largeish_temp(k) SELECT generate_series(1, 200)")
    for blk in (0, 2, 4):
        conn.sql(f"SELECT invalidate_rel_block('largeish_temp', {blk})")
    conn.sql("SELECT * FROM read_stream_for_blocks('largeish_temp', ARRAY[0, 2, 2, 4, 4])")
    conn.sql("SELECT * FROM read_stream_for_blocks('largeish_temp', ARRAY[0, 2, 2, 4, 4])")

    conn.close()


def test_inject_foreign(aio_node):
    """A read stream encountering buffers undergoing IO in another backend."""
    skip_unless_injection_points(aio_node)
    node = aio_node
    a = node.background()
    b = node.background()

    def b_waits_on(relfilenode_sql):
        b.sql(
            "SELECT inj_io_completion_wait(pid=>pg_backend_pid(), "
            f"relfilenode=>{relfilenode_sql})"
        )

    fn = "pg_relation_filenode('largeish')"
    stream = "SELECT array_agg(blocknum) FROM read_stream_for_blocks('largeish', ARRAY{})"

    # --- the other backend's read succeeds ---
    a.sql("SELECT evict_rel('largeish')")
    b_waits_on(fn)
    b_fut = wait_block(
        node, b, "SELECT read_rel_block_ll('largeish', blockno=>5, nblocks=>1)",
        "completion_wait", current_session=False,
    )
    # Block 5 is undergoing IO in b, so a moves on to start IO for block 7.
    a_fut = wait_block(node, a, stream.format("[0, 2, 5, 7]"), "AioIoCompletion")
    node.sql("SELECT inj_io_completion_continue()")
    b_fut.result()
    assert a_fut.result() == [0, 2, 5, 7]

    # --- the other backend's read fails ---
    a.sql("SELECT evict_rel('largeish')")
    b_waits_on(fn)
    b.sql(
        "SELECT inj_io_short_read_attach(-errno_from_string('EIO'), "
        f"pid=>pg_backend_pid(), relfilenode=>{fn})"
    )
    b_fut = wait_block(
        node, b, "SELECT read_rel_block_ll('largeish', blockno=>5, nblocks=>1)",
        "completion_wait", current_session=False,
    )
    a_fut = wait_block(node, a, stream.format("[0, 2, 5, 7]"), "AioIoCompletion")
    node.sql("SELECT inj_io_completion_continue()")
    assert a_fut.result() == [0, 2, 5, 7]
    with pytest.raises(LibpqError, match=r"could not read blocks 5\.\.5"):
        b_fut.result()
    b.sql("SELECT inj_io_short_read_detach()")

    # --- two buffers undergoing the same IO started by another backend ---
    a.sql("SELECT evict_rel('largeish')")
    b_waits_on(fn)
    b_fut = wait_block(
        node, b, "SELECT read_rel_block_ll('largeish', blockno=>2, nblocks=>3)",
        "completion_wait", current_session=False,
    )
    a_fut = wait_block(node, a, stream.format("[0, 2, 4]"), "AioIoCompletion")
    node.sql("SELECT inj_io_completion_continue()")
    b_fut.result()
    assert a_fut.result() == [0, 2, 4]

    a.quit()
    b.quit()
