# Copyright (c) 2026, PostgreSQL Global Development Group

"""Port of src/test/recovery/t/053_standby_login_event_trigger.pl.

Verify that connecting to a standby still works after a login event trigger
has been created and dropped on the primary.

CREATE EVENT TRIGGER ... ON login sets pg_database.dathasloginevt to true on
the primary, but DROP EVENT TRIGGER does not clear it -- the next login event
trigger pass clears the flag lazily on the primary.  That dangling flag
replicates to the standby.  Before the RecoveryInProgress() guard in
EventTriggerOnLogin(), the standby tried to clear the flag itself, which
requires AccessExclusiveLock on the database object; that lock mode is
forbidden during recovery, so the new connection died with FATAL.

To keep the test robust the event trigger is set up in a dedicated database
(regress_login_evt).  All synchronisation helpers connect to "postgres" on
the primary; if the trigger were created in "postgres" itself, that probe
connection would enter the cleanup branch on the primary and silently clear
the flag before the test even runs, making the scenario unreproducible.
"""


def test_standby_login_event_trigger(create_pg):
    primary = create_pg("primary", allows_streaming=True)
    backup = primary.backup("login_evt_backup")
    standby = create_pg("standby", from_backup=backup, streaming_primary=primary)

    # A dedicated database isolates the dangling dathasloginevt flag from any
    # helper that connects to the default "postgres" database.
    primary.sql("CREATE DATABASE regress_login_evt")
    primary.wait_for_catchup(standby)

    # Sanity check: the standby can connect to the new database before the
    # trigger machinery has touched it.
    standby.sql_oneshot("SELECT 1", dbname="regress_login_evt")

    # Create and drop a login event trigger inside the dedicated database in a
    # single session.  CREATE EVENT TRIGGER sets pg_database.dathasloginevt =
    # true for regress_login_evt; mark it ENABLE ALWAYS so the scenario matches
    # the original bug report.  After DROP the flag remains set on disk until a
    # subsequent login on the primary clears it; since later helpers only touch
    # "postgres", regress_login_evt's flag stays set and replicates that way to
    # the standby.
    primary.sql_batch_oneshot(
        """
        CREATE FUNCTION init_session() RETURNS event_trigger
        LANGUAGE plpgsql AS $$ BEGIN RAISE NOTICE 'init_session'; END $$;
        """,
        """
        CREATE EVENT TRIGGER init_session ON login
            EXECUTE FUNCTION init_session();
        """,
        "ALTER EVENT TRIGGER init_session ENABLE ALWAYS",
        "DROP EVENT TRIGGER init_session",
        "DROP FUNCTION init_session()",
        dbname="regress_login_evt",
    )

    # Wait for the standby to replay the CREATE/DROP catalog state.  This probes
    # "postgres", not regress_login_evt, so it does not disturb the dangling flag.
    primary.wait_for_catchup(standby)

    flag = "SELECT dathasloginevt FROM pg_database WHERE datname = 'regress_login_evt'"

    # The flag remains set in regress_login_evt on both sides.
    assert primary.sql(flag) is True, (
        "dathasloginevt remains set on primary after DROP EVENT TRIGGER"
    )
    assert standby.sql(flag) is True, "dathasloginevt replicated to standby"

    # A new connection to regress_login_evt on the standby exercises
    # EventTriggerOnLogin()'s cleanup branch.  With the RecoveryInProgress()
    # guard it succeeds; without it the session aborts with a FATAL about
    # AccessExclusiveLock.
    standby.sql_oneshot("SELECT 1", dbname="regress_login_evt")

    # Finally exercise the primary-side cleanup that the standby is meant to
    # defer to.  Opening a fresh session against regress_login_evt on the
    # primary enters EventTriggerOnLogin()'s cleanup branch with the trigger
    # list empty; AccessExclusiveLock is allowed outside recovery, so the flag
    # is cleared in place.  The in-place update emits a XLOG_HEAP_INPLACE record
    # but does not assign an xid or write a commit record, so the WAL is not
    # auto-flushed -- force a flush via pg_switch_wal() so the record reaches
    # the standby.
    primary.sql_oneshot("SELECT 1", dbname="regress_login_evt")
    assert primary.sql(flag) is False, (
        "primary clears dathasloginevt on next login after DROP"
    )

    primary.sql("SELECT pg_switch_wal()")
    primary.wait_for_catchup(standby)
    assert standby.sql(flag) is False, "cleared dathasloginevt replicates to standby"
