# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/test_aio/t/003_initdb.pl.

Tests initdb for each IO method. Kept separate from 001_aio because running a
real initdb per method isn't fast.
"""

import re

import pytest

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
    r = postgres.check_all("-C", "invalid", "-c", "io_method=invalid", exit_code=1)
    m = re.search(r"Available values: ([^.]+)\.", r.stderr)
    assert m, f"can't determine supported io_method values: {r.stderr}"
    return m.group(1)


@pytest.mark.parametrize("method", IO_METHODS)
def test_initdb(create_pg, method):
    if method not in _supported_io_methods():
        pytest.skip(f"io_method {method} not supported by this build")

    # A real initdb (bypassing the template) with the io_method persisted, so
    # the method is exercised during initdb itself.
    node = create_pg(
        f"initdb_{method}",
        initdb_opts=["-c", f"io_method={method}"],
        conf={**CONFIGURE, "io_method": method},
    )

    assert node.sql("SHOW io_method") == method
    node.stop()
