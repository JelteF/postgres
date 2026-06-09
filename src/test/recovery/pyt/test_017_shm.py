# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/017_shm.pl.

Tests of the pg_shmem.h shared-memory startup interlock: a postmaster picks a
SysV shmem key derived from its data directory's inode, copes with that key
being already taken, recycles its key after a crash, and (crucially) refuses to
start -- in both normal and single-user mode -- while a runaway backend from a
previous postmaster is still attached to the old shared memory segment.
"""

import ctypes
import os
import signal
import subprocess
import sys
import time

import pytest

from pypg._env import test_timeout_default

# SysV shared memory via libc, mirroring Perl's IPC::SharedMem.
IPC_CREAT = 0o1000
IPC_EXCL = 0o2000
IPC_RMID = 0

_libc = ctypes.CDLL("libc.so.6", use_errno=True)
_libc.shmget.restype = ctypes.c_int
_libc.shmget.argtypes = [ctypes.c_int, ctypes.c_size_t, ctypes.c_int]
_libc.shmctl.restype = ctypes.c_int
_libc.shmctl.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]

PRE_EXISTING = "pre-existing shared memory block"


def _create_conflicting_shm(inode):
    """Create a SysV shmem segment keyed on ``inode`` (the server's first-choice
    key), or return None if it already exists / can't be created."""
    key = ctypes.c_int(inode).value
    shmid = _libc.shmget(key, 1024, IPC_CREAT | IPC_EXCL | 0o600)
    return shmid if shmid >= 0 else None


def _remove_shm(shmid):
    if shmid is not None and shmid >= 0:
        _libc.shmctl(shmid, IPC_RMID, None)


def _postmaster_pid(node):
    """Read the current postmaster PID from postmaster.pid (start() only
    refreshes node.pid on its own, not after pg_ctl restart)."""
    with open(node.datadir / "postmaster.pid") as f:
        return int(f.readline().strip())


def _poll_start(node):
    """Retry start() until it succeeds, cleaning up between attempts. A new
    postmaster may need retries while the kernel delivers SIGKILL and the old
    children exit. Mirrors Perl's poll_start()."""
    for _ in range(10 * test_timeout_default()):
        try:
            node.start()
            return
        except subprocess.CalledProcessError:
            node.stop("fast")
            time.sleep(0.1)
    node.start()


def test_shm(create_pg, pg_bin):
    if sys.platform == "win32":
        pytest.skip("SysV shared memory not supported by this platform")

    gnat = create_pg("gnat", start=False)

    # Create a shmem segment that conflicts with gnat's first choice of shmem
    # key (derived from the data directory's inode). If it already exists,
    # that's fine -- the test then exercises a slightly different scenario.
    inode = os.stat(gnat.datadir).st_ino
    conflict_shm = _create_conflicting_shm(inode)

    gnat.start()
    gnat.pg_ctl("restart")  # should keep the same shmem key

    # Upon postmaster death its children exit automatically.
    os.kill(_postmaster_pid(gnat), signal.SIGKILL)
    _poll_start(gnat)  # gnat recycles its former shmem key

    # Remove the conflicting segment, crash again; gnat now uses its normal key
    # and fails (harmlessly) to remove the higher-keyed previous segment.
    _remove_shm(conflict_shm)
    os.kill(gnat.pid, signal.SIGKILL)
    _poll_start(gnat)
    gnat.stop()

    # Re-create the conflicting segment and start/stop normally, so the test
    # doesn't leak the higher-keyed segment.
    conflict_shm = _create_conflicting_shm(inode)
    gnat.start()
    gnat.stop()
    _remove_shm(conflict_shm)

    # Scenarios with no postmaster.pid, a dead postmaster, and a live backend.
    # A regress.c function emulates a CPU-intensive, responsive backend.
    gnat.start()
    regress_shlib = os.environ["REGRESS_SHLIB"]
    gnat.sql(
        f"CREATE FUNCTION wait_pid(int) RETURNS void "
        f"AS '{regress_shlib}' LANGUAGE C STRICT"
    )
    slow_query = "SELECT wait_pid(pg_backend_pid())"
    slow = gnat.background()
    slow_future = slow.asql(slow_query)
    try:
        gnat.poll_query_until(
            f"SELECT count(*) = 1 FROM pg_stat_activity WHERE query = '{slow_query}'"
        )
        slow_pid = gnat.sql(
            f"SELECT pid FROM pg_stat_activity WHERE query = '{slow_query}'"
        )

        os.kill(gnat.pid, signal.SIGKILL)
        (gnat.datadir / "postmaster.pid").unlink()

        # Reject ordinary startup: the live backend keeps the old shared memory
        # segment attached. Retry, as the message may take a moment to appear.
        offset = gnat.current_log_position()
        for _ in range(10 * test_timeout_default()):
            try:
                gnat.start()
                break
            except subprocess.CalledProcessError:
                pass
            if PRE_EXISTING in gnat.log_since(offset):
                break
            time.sleep(0.1)
        assert PRE_EXISTING in gnat.log_since(offset), (
            "detected live backend via shared memory"
        )

        # Reject single-user startup for the same reason.
        r = pg_bin.run(
            "postgres", "--single", "-D", str(gnat.datadir), "template1",
            input="", check=False,
        )
        assert r.returncode != 0 and PRE_EXISTING in r.stderr, (
            "single-user mode detected live backend via shared memory"
        )

        # Clean up the slow backend; now startup should work.
        gnat.pg_ctl("kill", "QUIT", str(slow_pid))
    finally:
        try:
            slow_future.result()
        except Exception:
            pass
        slow.close()

    _poll_start(gnat)
    gnat.stop()
