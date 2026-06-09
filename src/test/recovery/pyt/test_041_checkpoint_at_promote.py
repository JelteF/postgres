# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/041_checkpoint_at_promote.pl.

Tests the race where a restart point is running on a standby during its
promotion: the checkpointer is parked in the middle of a restart point with an
injection point, promotion is triggered, the checkpointer is then woken to
finish the restart point, and the newly-promoted node must survive a
subsequent crash-restart.
"""

import pytest

import pypg
from libpq import LibpqError
from pypg.util import wait_until

POINT = "create-restart-point"

DIED = (
    "server closed the connection unexpectedly"
    "|connection to server was lost"
    "|could not send data to server"
    "|no connection to the server"
)


def wait_for_reconnect(node):
    for _ in wait_until("server did not come back after crash", timeout=180):
        try:
            if node.sql("SELECT 1") == 1:
                return
        except LibpqError:
            pass


@pypg.require_injection_points()
def test_checkpoint_at_promote(create_pg):
    # log_checkpoints so restart-point activity can be observed in the logs.
    primary = create_pg(
        "master",
        allows_streaming=True,
        conf={"log_checkpoints": True, "restart_after_crash": True},
    )

    backup = primary.backup("my_backup")
    standby = create_pg("standby1", from_backup=backup, streaming_primary=primary)

    primary.sql("checkpoint")
    primary.sql("CREATE TABLE prim_tab (a int)")

    # Create the extension on the primary and let it replicate to the standby.
    primary.sql("CREATE EXTENSION injection_points")
    primary.wait_for_catchup(standby)

    # From here the checkpointer waits in the middle of a restart point.
    standby.sql(f"SELECT injection_points_attach('{POINT}', 'wait')")

    # Run a restart point in the background; it will block on the point.
    logstart = standby.current_log_position()
    restart_point = standby.background_sql_oneshot("CHECKPOINT")

    # Switch a WAL segment so the restart point will recycle it on completion.
    primary.sql("INSERT INTO prim_tab VALUES (1)")
    primary.sql("SELECT pg_switch_wal()")
    primary.wait_for_catchup(standby)

    # Wait until the checkpointer is parked in the restart point.
    standby.wait_for_event("checkpointer", POINT)
    assert "restartpoint starting: fast wait" in standby.log_since(logstart), (
        "restartpoint has started"
    )

    # Trigger promotion during the restart point.
    primary.stop()
    standby.promote()

    # Wake the checkpointer and wait for the restart point to complete on the
    # newly-promoted node. Capture the log position after promotion first.
    logstart = standby.current_log_position()
    standby.sql(f"SELECT injection_points_wakeup('{POINT}')")
    restart_point.result()
    standby.wait_for_log("restartpoint complete", logstart)

    # SIGKILL a backend to force a crash-restart; the server must come back.
    killme = standby.connect()
    pid = killme.sql("SELECT pg_backend_pid()")
    standby.pg_ctl("kill", "KILL", str(pid))
    with pytest.raises(LibpqError, match=DIED):
        killme.sql("SELECT 1")

    # The SIGKILL crashes and restarts the whole postmaster, taking down every
    # other backend with it -- including the one behind sql()'s cached
    # connection (used above for the injection-point calls). sql() never
    # reconnects on its own, so without this the loop below would keep
    # hitting the same dead connection and never see the server come back.
    standby.close_default_conn()

    wait_for_reconnect(standby)
    assert standby.sql("select 1") == 1, "psql select 1"
