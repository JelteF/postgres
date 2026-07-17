# Copyright (c) 2025-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/test_aio/t/001_aio.pl.

Exercises the AIO subsystem through the test_aio extension, once per supported
io_method. Where the Perl test matches psql stderr (WARNING/ERROR text) with
`psql_like`, this uses captured server messages (``pytest.warns`` /
``PostgresWarning``/``PostgresNotice``) and ``pytest.raises`` instead, and
asserts on real result values rather than psql's text output.
"""

import contextlib
import re
import warnings

import pytest

from libpq import LibpqError, PostgresMessage, PostgresWarning
from pypg import skip_unless_injection_points
from pypg.bins import postgres

IO_METHODS = ["worker", "io_uring", "sync"]

CONFIGURE = {
    "shared_preload_libraries": "test_aio",
    "log_min_messages": "DEBUG3",
    "log_statement": "all",
    "log_error_verbosity": "default",
    "restart_after_crash": False,
    "temp_buffers": 100,
}


def _supported_io_methods():
    # Probe the valid io_method values from the error message for an invalid
    # one. -C avoids the superuser check (needed when running as administrator
    # on Windows).
    r = postgres.check_all("-C", "invalid", "-c", "io_method=invalid", exit_code=1)
    m = re.search(r"Available values: ([^.]+)\.", r.stderr)
    assert m, f"can't determine supported io_method values: {r.stderr}"
    return m.group(1)


@pytest.fixture(scope="module", params=IO_METHODS)
def aio_node(request, create_pg_module):
    # One configured node per io_method, shared across this module's sub-tests
    # (mirrors the Perl test_io_method, which reuses a single node per method).
    method = request.param
    if method not in _supported_io_methods():
        pytest.skip(f"io_method {method} not supported by this build")

    conf = {**CONFIGURE, "io_method": method}
    if method == "sync":
        conf["io_max_concurrency"] = 4
    node = create_pg_module(f"aio_{method}", conf=conf)

    assert node.sql("SHOW io_method") == method, "io_method set correctly"

    node.sql("CREATE EXTENSION test_aio")
    node.sql(
        "CREATE TABLE tbl_corr(data int not null) WITH (AUTOVACUUM_ENABLED = false)"
    )
    node.sql("CREATE TABLE tbl_ok(data int not null) WITH (AUTOVACUUM_ENABLED = false)")
    node.sql("INSERT INTO tbl_corr SELECT generate_series(1, 10000)")
    node.sql("INSERT INTO tbl_ok SELECT generate_series(1, 10000)")
    node.sql("SELECT grow_rel('tbl_corr', 16)")
    node.sql("SELECT grow_rel('tbl_ok', 16)")
    node.sql("SELECT modify_rel_block('tbl_corr', 1, corrupt_header=>true)")
    node.sql("CHECKPOINT")
    return node


def wait_block(node, bg, sql, wait_event, current_session=True):
    """Dispatch ``sql`` on background session ``bg`` and wait until it parks on
    ``wait_event``, returning the Future. Mirrors the Perl query_wait_block:
    when ``current_session`` poll bg's own backend, else any backend."""
    if current_session:
        pid = bg.sql("SELECT pg_backend_pid()")
        fut = bg.background_sql(sql)
        node.poll_query_until(
            f"SELECT wait_event FROM pg_stat_activity WHERE pid = {pid}",
            expected=wait_event,
        )
    else:
        fut = bg.background_sql(sql)
        node.poll_query_until(
            "SELECT count(*) > 0 FROM pg_stat_activity "
            f"WHERE wait_event = '{wait_event}'",
            expected=True,
        )
    return fut


@contextlib.contextmanager
def no_messages():
    """Assert the server sends no NOTICE/WARNING during the block.

    Uses recording (not a ``"error"`` warning filter): a warning raised inside
    libpq's notice callback would be swallowed by ctypes rather than
    propagated, so we collect and check afterwards instead.
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        yield
    msgs = [str(w.message) for w in caught if issubclass(w.category, PostgresMessage)]
    assert not msgs, f"unexpected server message(s): {msgs}"


def test_handle(aio_node):
    """Sanity checks for the IO handle API."""

    def leaks():
        return pytest.warns(PostgresWarning, match="leaked AIO handle")

    # Leak warnings: an AIO handle obtained and not released warns at resource
    # owner cleanup, in implicit and explicit transactions and a subxact. The
    # warning is reported when the (sub)transaction ends, so the explicit cases
    # run their COMMIT/ROLLBACK inside the pytest.warns block.
    with leaks():
        aio_node.sql("SELECT handle_get()")
    with leaks():
        aio_node.sql("BEGIN")
        aio_node.sql("SELECT handle_get()")
        aio_node.sql("COMMIT")
    with leaks():
        aio_node.sql("BEGIN")
        aio_node.sql("SELECT handle_get()")
        aio_node.sql("ROLLBACK")
    with leaks():
        aio_node.sql("BEGIN")
        aio_node.sql("SAVEPOINT foo")
        aio_node.sql("SELECT handle_get()")
        aio_node.sql("COMMIT")

    # Leak warning + error: releasing in a different command is an unexpected
    # resource-owner state. The COMMIT both cleans up the aborted transaction
    # and is where the leaked handle is reported.
    with leaks():
        aio_node.sql("BEGIN")
        aio_node.sql("SELECT handle_get()")
        with pytest.raises(LibpqError, match="release in unexpected state"):
            aio_node.sql("SELECT handle_release_last()")
        aio_node.sql("COMMIT")

    # No leak: handle released in the same command, so nothing is warned.
    with no_messages():
        aio_node.sql("BEGIN")
        aio_node.sql("SELECT handle_get() UNION ALL SELECT handle_release_last()")
        aio_node.sql("COMMIT")
        aio_node.sql("SELECT handle_get_release()")

    # API violation: two handouts.
    with pytest.raises(
        LibpqError, match="API violation: Only one IO can be handed out"
    ):
        aio_node.sql("SELECT handle_get_twice()")

    # Error recovery: the session keeps working after an error, in an implicit
    # xact, an explicit xact and a subxact.
    with pytest.raises(LibpqError, match="as you command"):
        aio_node.sql("SELECT handle_get_and_error()")
    aio_node.sql("SELECT 'ok', handle_get_release()")

    aio_node.sql("BEGIN")
    with pytest.raises(LibpqError, match="as you command"):
        aio_node.sql("SELECT handle_get_and_error()")
    aio_node.sql("ROLLBACK")
    aio_node.sql("SELECT handle_get_release(), 'ok'")

    aio_node.sql("BEGIN")
    aio_node.sql("SAVEPOINT foo")
    with pytest.raises(LibpqError, match="as you command"):
        aio_node.sql("SELECT handle_get_and_error()")
    aio_node.sql("ROLLBACK TO SAVEPOINT foo")
    aio_node.sql("SELECT handle_get_release()")
    aio_node.sql("ROLLBACK")


def test_batchmode(aio_node):
    """Sanity checks for IO batchmode."""

    # Avoid printing a result tuple: in a RELCACHE_FORCE_RELEASE /
    # CATCACHE_FORCE_RELEASE build, the type lookup to print one would itself
    # start a batch.
    batch_start = "SELECT WHERE batch_start() IS NULL"

    def open_batch():
        return pytest.warns(PostgresWarning, match="open AIO batch at end")

    # Leak warning & recovery, implicit and explicit xact.
    with open_batch():
        aio_node.sql(batch_start)
    with open_batch():
        aio_node.sql("BEGIN")
        aio_node.sql(batch_start)
        aio_node.sql("COMMIT")

    # No warning: batch closed in the same command.
    with no_messages():
        aio_node.sql(f"{batch_start} UNION ALL SELECT WHERE batch_end() IS NULL")


def test_io_error(aio_node):
    """Simple cases of invalid pages are reported."""
    conn = aio_node.connect()

    # A temporary table must be corrupted and read in the same session.
    conn.sql("CREATE TEMPORARY TABLE tmp_corr(data int not null)")
    conn.sql("INSERT INTO tmp_corr SELECT generate_series(1, 10000)")
    conn.sql("SELECT modify_rel_block('tmp_corr', 1, corrupt_header=>true)")

    for tbl, page_re in [
        ("tbl_corr", r'invalid page in block 1 of relation "base/\d+/\d+'),
        ("tmp_corr", r'invalid page in block 1 of relation "base/\d+/t\d+_\d+'),
    ]:
        # custom C code (low-level read)
        with pytest.raises(LibpqError, match=page_re):
            conn.sql(f"SELECT read_rel_block_ll('{tbl}', 1)")
        # bufmgr read, sequential scan
        with pytest.raises(LibpqError, match=page_re):
            conn.sql(f"SELECT count(*) FROM {tbl}")
        # bufmgr read, tid scan
        with pytest.raises(LibpqError, match=page_re):
            conn.sql(f"SELECT count(*) FROM {tbl} WHERE ctid = '(1, 1)'")

    conn.close()


def test_complete_foreign(aio_node):
    """A read started but not awaited by one backend can be completed by
    another (or after the starting backend exits)."""
    node = aio_node
    a = node.connect()
    b = node.connect()

    # Issue IO without waiting for completion, then let another backend read.
    a.sql("SELECT read_rel_block_ll('tbl_ok', 1, wait_complete=>false)")
    with no_messages():
        assert b.sql("SELECT count(*) FROM tbl_ok WHERE ctid = '(1,1)' LIMIT 1") == 1

    # Issue IO without waiting, then exit; another backend must still read it,
    # proving the exiting backend left the AIO in a sane state.
    a.sql("SELECT read_rel_block_ll('tbl_ok', 1, wait_complete=>false)")
    a.close()
    a = node.connect()
    with no_messages():
        assert b.sql("SELECT count(*) FROM tbl_ok WHERE ctid = '(1,1)' LIMIT 1") == 1

    # Read of a corrupt block started in the background is logged as LOG; the
    # foreground SELECT that completes it errors.
    offset = node.current_log_position()
    a.sql("SELECT read_rel_block_ll('tbl_corr', 1, wait_complete=>false)")
    with pytest.raises(LibpqError, match="invalid page in block"):
        b.sql("SELECT count(*) FROM tbl_corr WHERE ctid = '(1,1)' LIMIT 1")
    node.wait_for_log(r"LOG[^\n]+invalid page in", offset)
    node.wait_for_log(r"ERROR[^\n]+invalid page in", offset)

    a.close()
    b.close()


def test_close_fd(aio_node):
    """FDs being closed while IO is in progress is handled."""
    conn = aio_node.connect()

    with no_messages():
        conn.sql(
            "SELECT read_rel_block_ll('tbl_ok', 1, wait_complete=>true, "
            "batchmode_enter=>true, smgrreleaseall=>true, batchmode_exit=>true)"
        )
        conn.sql(
            "SELECT read_rel_block_ll('tbl_ok', 1, wait_complete=>false, "
            "batchmode_enter=>true, smgrreleaseall=>true, batchmode_exit=>true)"
        )
        assert conn.sql("SELECT count(*) FROM tbl_ok WHERE ctid = '(1,1)' LIMIT 1") == 1

    conn.close()


def test_invalidate(aio_node):
    """A relation removed (rollback or DROP) while IO is ongoing is handled."""
    conn = aio_node.connect()

    for persistency in ("normal", "unlogged", "temporary"):
        kind = "" if persistency == "normal" else persistency
        tbl = f"{persistency}_transactional"
        create = (
            f"CREATE {kind} TABLE {tbl} (id int not null, data text not null) "
            "WITH (AUTOVACUUM_ENABLED = false)"
        )
        insert = (
            f"INSERT INTO {tbl}(id, data) "
            "SELECT generate_series(1, 10000) as id, repeat('a', 200)"
        )

        # Outstanding read IO must not break AbortTransaction cleanup.
        conn.sql("BEGIN")
        conn.sql(create)
        conn.sql(insert)
        conn.sql(f"SELECT read_rel_block_ll('{tbl}', 1, wait_complete=>false)")
        with no_messages():
            conn.sql("ROLLBACK")

        # ... nor CommitTransaction cleanup on DROP.
        conn.sql("BEGIN")
        conn.sql(create)
        conn.sql(insert)
        conn.sql("COMMIT")
        conn.sql("BEGIN")
        conn.sql(f"SELECT read_rel_block_ll('{tbl}', 1, wait_complete=>false)")
        with no_messages():
            conn.sql(f"DROP TABLE {tbl}")
            conn.sql("COMMIT")

    conn.close()


def test_inject(aio_node):
    """Hard IO errors that are hard to trigger without injection points."""
    skip_unless_injection_points()
    conn = aio_node.connect()

    base_err = r'could not read blocks 2\.\.2 in file "base/.*"'

    # A short read that still returns the whole block is fine.
    conn.sql("SELECT inj_io_short_read_attach(8192)")
    conn.sql("SELECT invalidate_rel_block('tbl_ok', 2)")
    with no_messages():
        assert conn.sql("SELECT count(*) FROM tbl_ok WHERE ctid = '(2, 1)'") == 1

    # A read shorter than a block errors.
    conn.sql("SELECT inj_io_short_read_attach(17)")
    conn.sql("SELECT invalidate_rel_block('tbl_ok', 2)")
    with pytest.raises(LibpqError, match=base_err + ": read only 0 of 8192 bytes"):
        conn.sql("SELECT count(*) FROM tbl_ok WHERE ctid = '(2, 1)'")

    inval = [
        f"SELECT invalidate_rel_block('tbl_ok', {b})" for b in (0, 1, 2, 3, 5, 6, 7, 8)
    ]

    # A multi-block read shortened to one block is retried.
    conn.sql_batch(*inval)
    conn.sql("SELECT inj_io_short_read_attach(8192)")
    with no_messages():
        assert conn.sql("SELECT count(*) FROM tbl_ok") == 10000

    # ... and shortened to two blocks.
    conn.sql_batch(*inval)
    conn.sql("SELECT inj_io_short_read_attach(8192*2)")
    with no_messages():
        assert conn.sql("SELECT count(*) FROM tbl_ok") == 10000

    # Page verification errors are detected even in a shortened multi-block read.
    conn.sql_batch(
        "SELECT invalidate_rel_block('tbl_corr', 0)",
        "SELECT invalidate_rel_block('tbl_corr', 1)",
        "SELECT invalidate_rel_block('tbl_corr', 2)",
        "SELECT inj_io_short_read_attach(8192)",
    )
    with pytest.raises(
        LibpqError, match=r'invalid page in block 1 of relation "base/.*'
    ):
        conn.sql("SELECT count(*) FROM tbl_corr WHERE ctid < '(2, 1)'")

    # A hard EIO error is reported and recovered from.
    conn.sql("SELECT inj_io_short_read_attach(-errno_from_string('EIO'))")
    conn.sql("SELECT invalidate_rel_block('tbl_ok', 2)")
    eio = base_err + r": (?:I/O|Input/output) error"
    with pytest.raises(LibpqError, match=eio):
        conn.sql("SELECT count(*) FROM tbl_ok")
    with pytest.raises(LibpqError, match=eio):
        conn.sql("SELECT count(*) FROM tbl_ok")
    conn.sql("SELECT inj_io_short_read_detach()")
    with no_messages():
        assert conn.sql("SELECT count(*) FROM tbl_ok") == 10000

    # A different hard error (EROFS).
    conn.sql("SELECT inj_io_short_read_attach(-errno_from_string('EROFS'))")
    conn.sql("SELECT invalidate_rel_block('tbl_ok', 2)")
    with pytest.raises(LibpqError, match=base_err + ": Read-only file system"):
        conn.sql("SELECT count(*) FROM tbl_ok")
    conn.sql("SELECT inj_io_short_read_detach()")

    conn.close()


def test_inject_worker(aio_node):
    """io_method=worker must recover from a failure to reopen a file."""
    if aio_node.sql("SHOW io_method") != "worker":
        pytest.skip("worker-specific test")
    skip_unless_injection_points()
    conn = aio_node.connect()

    conn.sql("SELECT inj_io_reopen_attach()")
    conn.sql("SELECT invalidate_rel_block('tbl_ok', 1)")
    with pytest.raises(
        LibpqError,
        match=r'could not read blocks 1\.\.1 in file "base/.*": No such file or directory',
    ):
        conn.sql("SELECT count(*) FROM tbl_ok")
    conn.sql("SELECT inj_io_reopen_detach()")
    with no_messages():
        assert conn.sql("SELECT count(*) FROM tbl_ok") == 10000

    conn.close()


def _checksum_count(conn, datname):
    where = f"datname = '{datname}'" if datname else "datname IS NULL"
    return conn.sql(f"SELECT checksum_failures FROM pg_stat_database WHERE {where}")


def _assert_checksum_increased(node, before, datname):
    """Wait until the database's checksum_failures has risen past ``before``
    and a failure timestamp is recorded (stats flush asynchronously)."""
    where = f"datname = '{datname}'" if datname else "datname IS NULL"
    node.poll_query_until(
        f"SELECT checksum_failures >= {before} + 1 "
        "AND checksum_last_failure IS NOT NULL "
        f"FROM pg_stat_database WHERE {where}",
        expected=True,
    )


def _start(buf, wait):
    return f"SELECT buffer_call_start_io({buf}, for_input=>true, wait=>{wait})"


def _terminate(buf, succeed):
    return (
        f"SELECT buffer_call_terminate_io({buf}, for_input=>true, "
        f"succeed=>{succeed}, io_error=>false, release_aio=>false)"
    )


def test_startwait_io(aio_node):
    """Interplay between StartBufferIO and TerminateBufferIO."""
    node = aio_node
    a = node.connect()
    b = node.connect()

    # --- normal table: IO_IN_PROGRESS is shared across sessions ---
    buf = a.sql("SELECT buffer_create_toy('tbl_ok', 1)")
    assert a.sql(_start(buf, "true")) is True
    # A second StartBufferIO fails, in the same and another session.
    assert a.sql(_start(buf, "false")) is False
    assert b.sql(_start(buf, "false")) is False

    # Starting IO in another session blocks; terminating without marking valid
    # lets the waiter start the IO.
    fut = wait_block(node, b, _start(buf, "true"), "BufferIo")
    a.sql(_terminate(buf, "false"))
    assert fut.result() is True
    b.sql(_terminate(buf, "false"))

    # Same again, but mark the IO successful: the waiter then needs no IO.
    assert a.sql(_start(buf, "true")) is True
    fut = wait_block(node, b, _start(buf, "true"), "BufferIo")
    a.sql(_terminate(buf, "true"))
    assert fut.result() is False

    a.sql("SELECT buffer_create_toy('tbl_ok', 1)")  # make invalid again

    # --- temporary table: local buffers don't use IO_IN_PROGRESS ---
    a.sql("CREATE TEMPORARY TABLE tmp_ok(data int not null)")
    a.sql("INSERT INTO tmp_ok SELECT generate_series(1, 10000)")
    buf = a.sql("SELECT buffer_create_toy('tmp_ok', 3)")
    assert a.sql(_start(buf, "false")) is True
    # A second StartLocalBufferIO also succeeds (documents that fact).
    assert a.sql(_start(buf, "false")) is True
    a.sql(_terminate(buf, "false"))
    assert a.sql(_start(buf, "false")) is True
    a.sql(_terminate(buf, "true"))
    # Now it fails because the buffer is already valid.
    assert a.sql(_start(buf, "true")) is False

    a.close()
    b.close()


def test_read_buffers(aio_node):
    """Tests for StartReadBuffers()."""
    node = aio_node
    a = node.connect()
    b = node.connect()
    a.sql("CREATE TEMPORARY TABLE tmp_ok(data int not null)")
    a.sql("INSERT INTO tmp_ok SELECT generate_series(1, 5000)")

    cols = "blockoff, blocknum, io_reqd, nblocks"
    # io_reqd masked by foreign IO, for the in-progress cases.
    cols_nf = "blockoff, blocknum, io_reqd and not foreign_io, nblocks"

    def rb(table, start, n, c=cols):
        return a.sql(f"SELECT {c} FROM read_buffers('{table}', {start}, {n})")

    for table in ("tbl_ok", "tmp_ok"):
        # consecutive misses combine into one read
        a.sql(f"SELECT evict_rel('{table}')")
        assert rb(table, 0, 2) == (0, 0, True, 2)
        # the same range now hits the buffer pool: two separate ops
        assert rb(table, 0, 2) == [(0, 0, False, 1), (1, 1, False, 1)]
        # a larger read interrupted by a hit
        assert rb(table, 3, 1) == (0, 3, True, 1)
        assert rb(table, 2, 4) == [(0, 2, True, 1), (1, 3, False, 1), (2, 4, True, 2)]

        # a read with an initial buffer hit
        a.sql(f"SELECT evict_rel('{table}')")
        assert rb(table, 0, 1) == (0, 0, True, 1)
        assert rb(table, 0, 1) == (0, 0, False, 1)
        assert rb(table, 1, 1) == (0, 1, True, 1)
        assert rb(table, 1, 1) == (0, 1, False, 1)
        assert rb(table, 0, 2) == [(0, 0, False, 1), (1, 1, False, 1)]
        assert rb(table, 0, 3) == [(0, 0, False, 1), (1, 1, False, 1), (2, 2, True, 1)]

        # an initial miss with trailing hit(s)
        a.sql(f"SELECT invalidate_rel_block('{table}', 0)")
        assert rb(table, 0, 3) == [(0, 0, True, 1), (1, 1, False, 1), (2, 2, False, 1)]
        a.sql(f"SELECT invalidate_rel_block('{table}', 1)")
        a.sql(f"SELECT invalidate_rel_block('{table}', 2)")
        a.sql(f"SELECT * FROM read_buffers('{table}', 3, 2)")
        assert rb(table, 1, 4) == [(0, 1, True, 2), (2, 3, False, 1), (3, 4, False, 1)]

        # io_combine_limit caps the read size
        a.sql(f"SELECT evict_rel('{table}')")
        a.sql("SET io_combine_limit=3")
        assert rb(table, 1, 5) == [(0, 1, True, 3), (3, 4, True, 2)]
        a.sql("RESET io_combine_limit")

        # in-progress IO at the first, middle and last block of the range
        a.sql(f"SELECT evict_rel('{table}')")
        a.sql(f"SELECT read_rel_block_ll('{table}', 1, wait_complete=>false)")
        assert rb(table, 1, 3, cols_nf) == [(0, 1, False, 1), (1, 2, True, 2)]
        a.sql(f"SELECT evict_rel('{table}')")
        a.sql(f"SELECT read_rel_block_ll('{table}', 2, wait_complete=>false)")
        assert rb(table, 1, 3, cols_nf) == [
            (0, 1, True, 1),
            (1, 2, False, 1),
            (2, 3, True, 1),
        ]
        a.sql(f"SELECT evict_rel('{table}')")
        a.sql(f"SELECT read_rel_block_ll('{table}', 3, wait_complete=>false)")
        assert rb(table, 1, 3, cols_nf) == [(0, 1, True, 2), (2, 3, False, 1)]

    # The remaining cases need multiple sessions; sync can't be observed
    # because it does not start IO in StartReadBuffers().
    table = "tbl_ok"
    fcols = "blockoff, blocknum, io_reqd, foreign_io, nblocks"
    if node.sql("SHOW io_method") != "sync":
        # IO is split around a concurrent failed IO.
        a.sql(f"SELECT evict_rel('{table}')")
        buf = b.sql(f"SELECT buffer_create_toy('{table}', 3)")
        b.sql(_start(buf, "true"))
        fut = wait_block(
            node, a, f"SELECT {fcols} FROM read_buffers('{table}', 1, 5)", "BufferIo"
        )
        b.sql(_terminate(buf, "false"))
        assert fut.result() == [(0, 1, True, False, 2), (2, 3, True, False, 3)]

        # ... and around a concurrent successful IO.
        a.sql(f"SELECT evict_rel('{table}')")
        buf = b.sql(f"SELECT buffer_create_toy('{table}', 3)")
        b.sql(_start(buf, "true"))
        fut = wait_block(
            node, a, f"SELECT {fcols} FROM read_buffers('{table}', 1, 5)", "BufferIo"
        )
        b.sql(_terminate(buf, "true"))
        assert fut.result() == [
            (0, 1, True, False, 2),
            (2, 3, False, False, 1),
            (3, 4, True, False, 2),
        ]

    a.close()
    b.close()


def test_zero(aio_node):
    """Behavior of ZERO_ON_ERROR and zero_damaged_pages."""
    node = aio_node
    a = node.connect()
    b = node.connect()

    for persistency in ("normal", "temporary"):
        kind = "" if persistency == "normal" else persistency
        a.sql(f"CREATE {kind} TABLE tbl_zero(id int) WITH (AUTOVACUUM_ENABLED = false)")
        a.sql("INSERT INTO tbl_zero SELECT generate_series(1, 10000)")

        a.sql("SELECT modify_rel_block('tbl_zero', 0, corrupt_header=>true)")

        # A page validity error is reported,
        with pytest.raises(LibpqError, match=r"invalid page in block 0 of relation"):
            a.sql("SELECT read_rel_block_ll('tbl_zero', 0, zero_on_error=>false)")
        # ... or zeroed with a warning under zero_on_error.
        with pytest.warns(
            PostgresWarning,
            match=r"invalid page in block 0 of relation .*; zeroing out page",
        ):
            a.sql("SELECT read_rel_block_ll('tbl_zero', 0, zero_on_error=>true)")

        # Once fixed, the block reads cleanly.
        a.sql("SELECT modify_rel_block('tbl_zero', 0, zero=>true)")
        with no_messages():
            a.sql("SELECT read_rel_block_ll('tbl_zero', 0, zero_on_error=>false)")

        # The correct block number is reported for a different block.
        a.sql("SELECT modify_rel_block('tbl_zero', 3, corrupt_header=>true)")
        with pytest.warns(
            PostgresWarning,
            match=r"invalid page in block 3 of relation .*; zeroing out page",
        ):
            a.sql("SELECT read_rel_block_ll('tbl_zero', 3, zero_on_error=>true)")

        # One read reporting multiple invalid blocks.
        a.sql("SELECT modify_rel_block('tbl_zero', 2, corrupt_header=>true)")
        a.sql("SELECT modify_rel_block('tbl_zero', 3, corrupt_header=>true)")
        with pytest.raises(
            LibpqError, match=r"2 invalid pages among blocks 1\.\.4 of relation"
        ):
            a.sql(
                "SELECT read_rel_block_ll('tbl_zero', 1, nblocks=>4, zero_on_error=>false)"
            )
        # zeroed via the ZERO_ON_ERROR flag ...
        with pytest.warns(
            PostgresWarning,
            match=r"zeroing out 2 invalid pages among blocks 1\.\.4 of relation",
        ):
            a.sql(
                "SELECT read_rel_block_ll('tbl_zero', 1, nblocks=>4, zero_on_error=>true)"
            )
        # ... and via zero_damaged_pages.
        a.sql("BEGIN")
        a.sql("SET LOCAL zero_damaged_pages = true")
        with pytest.warns(
            PostgresWarning,
            match=r"zeroing out 2 invalid pages among blocks 1\.\.4 of relation",
        ):
            a.sql(
                "SELECT read_rel_block_ll('tbl_zero', 1, nblocks=>4, zero_on_error=>false)"
            )
        a.sql("COMMIT")

        # bufmgr IO detects page validity errors.
        a.sql(
            "SELECT invalidate_rel_block('tbl_zero', g.i) FROM generate_series(0, 15) g(i)"
        )
        a.sql("SELECT modify_rel_block('tbl_zero', 3, zero=>true)")
        with pytest.raises(LibpqError, match=r"invalid page in block 2 of relation"):
            a.sql("SELECT count(*) FROM tbl_zero")
        # ... and zeroes them with zero_damaged_pages.
        a.sql("BEGIN")
        a.sql("SET LOCAL zero_damaged_pages = true")
        with pytest.warns(
            PostgresWarning, match=r"invalid page in block 2 of relation"
        ):
            a.sql("SELECT count(*) FROM tbl_zero")
        a.sql("COMMIT")

        # A page validity error in an IO that session B completes must not be
        # logged visibly to B. Needs cross-session access, so non-temp only.
        if persistency != "temporary":
            a.sql("SELECT modify_rel_block('tbl_zero', 1, corrupt_header=>true)")
            a.sql(
                "SELECT read_rel_block_ll('tbl_zero', 1, wait_complete=>false, "
                "zero_on_error=>true)"
            )
            with no_messages():
                assert b.sql("SELECT count(*) > 0 FROM tbl_zero") is True

        a.sql("DROP TABLE tbl_zero")

    a.close()
    b.close()


def test_checksum(aio_node):
    """Checksum failures are detected and reported in the stats."""
    node = aio_node
    a = node.connect()

    a.sql("CREATE TABLE tbl_normal(id int) WITH (AUTOVACUUM_ENABLED = false)")
    a.sql("INSERT INTO tbl_normal SELECT generate_series(1, 5000)")
    a.sql("SELECT modify_rel_block('tbl_normal', 3, corrupt_checksum=>true)")
    a.sql("CREATE TEMPORARY TABLE tbl_temp(id int) WITH (AUTOVACUUM_ENABLED = false)")
    a.sql("INSERT INTO tbl_temp SELECT generate_series(1, 5000)")
    a.sql("SELECT modify_rel_block('tbl_temp', 3, corrupt_checksum=>true)")
    a.sql("SELECT modify_rel_block('tbl_temp', 4, corrupt_checksum=>true)")

    # A shared rel with invalid pages: pg_shseclabel isn't accessed by default.
    a.sql("SELECT grow_rel('pg_shseclabel', 4)")
    a.sql("SELECT modify_rel_block('pg_shseclabel', 2, corrupt_checksum=>true)")
    a.sql("SELECT modify_rel_block('pg_shseclabel', 3, corrupt_checksum=>true)")

    # normal rel
    before = _checksum_count(a, "postgres")
    with pytest.raises(
        LibpqError, match=r'invalid page in block 3 of relation "base/\d+/\d+"'
    ):
        a.sql(
            "SELECT read_rel_block_ll('tbl_normal', 3, nblocks=>1, zero_on_error=>false)"
        )
    _assert_checksum_increased(node, before, "postgres")

    # temp rel
    before = _checksum_count(a, "postgres")
    with pytest.raises(
        LibpqError, match=r'invalid page in block 4 of relation "base/\d+/t\d+_\d+"'
    ):
        a.sql(
            "SELECT read_rel_block_ll('tbl_temp', 4, nblocks=>2, zero_on_error=>false)"
        )
    _assert_checksum_increased(node, before, "postgres")

    # shared rel
    before = _checksum_count(a, None)
    with pytest.raises(
        LibpqError,
        match=r'2 invalid pages among blocks 2\.\.3 of relation "global/\d+"',
    ):
        a.sql(
            "SELECT read_rel_block_ll('pg_shseclabel', 2, nblocks=>2, zero_on_error=>false)"
        )
    _assert_checksum_increased(node, before, None)

    # restore sanity
    a.sql("SELECT modify_rel_block('pg_shseclabel', 1, zero=>true)")
    a.sql("DROP TABLE tbl_normal")
    a.close()


def test_ignore_checksum(aio_node):
    """ignore_checksum_failure handling, including multi-block reads."""
    node = aio_node
    conn = node.connect()

    conn.sql("CREATE TABLE tbl_cs_fail(id int) WITH (AUTOVACUUM_ENABLED = false)")
    conn.sql("INSERT INTO tbl_cs_fail SELECT generate_series(1, 10000)")
    count_sql = "SELECT count(*) FROM tbl_cs_fail"
    invalidate = "SELECT invalidate_rel_block('tbl_cs_fail', g.i) FROM generate_series(0, 6) g(i)"
    expect = conn.sql(count_sql)

    conn.sql("SELECT modify_rel_block('tbl_cs_fail', 1, corrupt_checksum=>true)")
    conn.sql("SELECT modify_rel_block('tbl_cs_fail', 5, corrupt_checksum=>true)")
    conn.sql("SELECT modify_rel_block('tbl_cs_fail', 6, corrupt_checksum=>true)")

    # off: a wrong checksum errors.
    conn.sql(invalidate)
    with pytest.raises(LibpqError, match=r"invalid page in block"):
        conn.sql(count_sql)

    # on: it is ignored with a warning.
    conn.sql("SET ignore_checksum_failure=on")
    conn.sql(invalidate)
    with pytest.warns(
        PostgresWarning, match=r"ignoring (checksum failure|\d checksum failures)"
    ):
        assert conn.sql(count_sql) == expect

    # ignore in a multi-block read still surfaces a real invalid page as ERROR.
    conn.sql("SELECT modify_rel_block('tbl_cs_fail', 2, zero=>true)")
    conn.sql("SELECT modify_rel_block('tbl_cs_fail', 3, corrupt_checksum=>true)")
    conn.sql("SELECT modify_rel_block('tbl_cs_fail', 4, corrupt_header=>true)")
    offset = node.current_log_position()
    with pytest.warns(PostgresWarning, match=r"ignoring checksum failure in block 3"):
        conn.sql(
            "SELECT read_rel_block_ll('tbl_cs_fail', 3, nblocks=>1, zero_on_error=>false)"
        )
    node.wait_for_log(r"LOG:  ignoring checksum failure", offset)
    with pytest.raises(
        LibpqError, match=r'invalid page in block 4 of relation "base/\d+/\d+"'
    ):
        conn.sql(
            "SELECT read_rel_block_ll('tbl_cs_fail', 2, nblocks=>3, zero_on_error=>false)"
        )

    # multi-block read with different problems in different blocks, zeroed.
    conn.sql("SELECT modify_rel_block('tbl_cs_fail', 1, zero=>true)")
    conn.sql("SELECT modify_rel_block('tbl_cs_fail', 2, corrupt_checksum=>true)")
    conn.sql(
        "SELECT modify_rel_block('tbl_cs_fail', 3, corrupt_checksum=>true, corrupt_header=>true)"
    )
    conn.sql("SELECT modify_rel_block('tbl_cs_fail', 4, corrupt_header=>true)")
    conn.sql("SELECT modify_rel_block('tbl_cs_fail', 5, corrupt_header=>true)")
    offset = node.current_log_position()
    with pytest.warns(
        PostgresWarning,
        match=r"zeroing 3 page\(s\) and ignoring 2 checksum failure\(s\) "
        r"among blocks 1\.\.5 of relation",
    ):
        conn.sql(
            "SELECT read_rel_block_ll('tbl_cs_fail', 1, nblocks=>5, zero_on_error=>true)"
        )
    node.wait_for_log(r"LOG:  ignoring checksum failure in block 2", offset)
    node.wait_for_log(
        r'LOG:  invalid page in block 3 of relation "base.*"; zeroing out page', offset
    )
    node.wait_for_log(
        r'LOG:  invalid page in block 4 of relation "base.*"; zeroing out page', offset
    )
    node.wait_for_log(
        r'LOG:  invalid page in block 5 of relation "base.*"; zeroing out page', offset
    )

    # both an invalid header and an invalid checksum in one block
    conn.sql(
        "SELECT modify_rel_block('tbl_cs_fail', 3, corrupt_checksum=>true, corrupt_header=>true)"
    )
    with pytest.raises(LibpqError, match=r'invalid page in block 3 of relation "'):
        conn.sql(
            "SELECT read_rel_block_ll('tbl_cs_fail', 3, nblocks=>1, zero_on_error=>false)"
        )
    with pytest.warns(
        PostgresWarning,
        match=r'invalid page in block 3 of relation "base/.*"; zeroing out page',
    ):
        conn.sql(
            "SELECT read_rel_block_ll('tbl_cs_fail', 3, nblocks=>1, zero_on_error=>true)"
        )

    conn.close()


def test_checksum_createdb(aio_node):
    """Checksum handling when creating a database from one with an invalid
    block (also a minimal cross-database IO check)."""
    node = aio_node
    node.sql("CREATE DATABASE regression_createdb_source")
    src = node.connect(dbname="regression_createdb_source")
    src.sql("CREATE EXTENSION test_aio")
    src.sql(
        "CREATE TABLE tbl_cs_fail(data int not null) WITH (AUTOVACUUM_ENABLED = false)"
    )
    src.sql("INSERT INTO tbl_cs_fail SELECT generate_series(1, 1000)")
    src.sql("SELECT modify_rel_block('tbl_cs_fail', 1, corrupt_checksum=>true)")
    src.close()

    createdb = (
        "CREATE DATABASE regression_createdb_target "
        "TEMPLATE regression_createdb_source STRATEGY wal_log"
    )
    conn = node.connect()

    # An invalid source block fails the create and is accounted for.
    before = _checksum_count(conn, "regression_createdb_source")
    with pytest.raises(
        LibpqError, match=r'invalid page in block 1 of relation "base/\d+/\d+"'
    ):
        conn.sql(createdb)
    _assert_checksum_increased(node, before, "regression_createdb_source")

    # Once the source is fixed, the create succeeds.
    src = node.connect(dbname="regression_createdb_source")
    src.sql("SELECT modify_rel_block('tbl_cs_fail', 1, zero=>true)")
    src.close()
    with no_messages():
        conn.sql(createdb)

    conn.close()


def test_read_buffers_inject(aio_node):
    """StartReadBuffers() recognizing another backend's in-progress IO as
    foreign IO, using injection points to hold an IO in its completion hook."""
    skip_unless_injection_points()
    node = aio_node
    a = node.connect()
    b = node.connect()
    c = node.connect()
    table = "tbl_ok"
    sync = node.sql("SHOW io_method") == "sync"
    fcols = "blockoff, blocknum, io_reqd, foreign_io, nblocks"

    def configure_wait(blockno):
        b.sql(
            "SELECT inj_io_completion_wait(pid=>pg_backend_pid(), "
            f"relfilenode=>pg_relation_filenode('{table}'), blockno=>{blockno})"
        )

    # Foreign IO on the first block of the range.
    a.sql(f"SELECT evict_rel('{table}')")
    configure_wait(1)
    b_fut = wait_block(
        node,
        b,
        f"SELECT read_rel_block_ll('{table}', blockno=>1, nblocks=>1)",
        "completion_wait",
        current_session=False,
    )
    a_fut = wait_block(
        node, a, f"SELECT {fcols} FROM read_buffers('{table}', 1, 4)", "AioIoCompletion"
    )
    c.sql("SELECT inj_io_completion_continue()")
    b_fut.result()
    if sync:
        # sync doesn't issue IO below StartReadBuffers(): one combined read.
        assert a_fut.result() == (0, 1, True, False, 4)
    else:
        # a foreign IO covering block 1, plus one covering blocks 2-4.
        assert a_fut.result() == [(0, 1, True, True, 1), (1, 2, True, False, 3)]

    # Foreign IO encountered multiple times (blocks 2 and 3).
    a.sql(f"SELECT evict_rel('{table}')")
    configure_wait(3)
    b_fut = wait_block(
        node,
        b,
        f"SELECT read_rel_block_ll('{table}', blockno=>2, nblocks=>2)",
        "completion_wait",
        current_session=False,
    )
    a_fut = wait_block(
        node, a, f"SELECT {fcols} FROM read_buffers('{table}', 0, 4)", "AioIoCompletion"
    )
    c.sql("SELECT inj_io_completion_continue()")
    b_fut.result()
    if sync:
        assert a_fut.result() == (0, 0, True, False, 4)
    else:
        # one IO for blocks 0-1, then foreign IOs for blocks 2 and 3.
        assert a_fut.result() == [
            (0, 0, True, False, 2),
            (2, 2, True, True, 1),
            (3, 3, True, True, 1),
        ]

    a.close()
    b.close()
    c.close()
