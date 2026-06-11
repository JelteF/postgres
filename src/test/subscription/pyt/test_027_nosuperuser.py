# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/027_nosuperuser.pl.

Tests that logical replication respects permissions. Tables are owned and
published by non-superuser regress_alice but replicated by regress_admin:
replication works while the apply worker's owner is superuser or has privileges
on the table owner's role, and fails (logging an ERROR) when superuser is
revoked, when row-level security is forced, or when the table owner lacks the
required INSERT/UPDATE/DELETE/SELECT privilege. Also checks the apply worker
restarts when the owner's superuser is revoked, and that a non-superuser
subscription owner must supply a password in the connection string.
"""

import sys

from libpq import LibpqError
from pypg._env import test_timeout_default

import pytest

# The password sub-test requires md5 over a Unix-domain socket connection
# (a "local" pg_hba rule), which the framework only uses on non-Windows
# platforms.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="requires Unix-domain sockets"
)

CANNOT_SET_ROLE = r'(?is)ERROR: ( [A-Z0-9]+:)? role "regress_admin" cannot SET ROLE to "regress_alice"'
RLS_FORCED = (
    r'(?is)ERROR: ( [A-Z0-9]+:)? user "regress_alice" cannot replicate into relation '
    r'with row-level security enabled: "unpartitioned\w*"'
)
PERM_DENIED = r"(?is)ERROR: ( [A-Z0-9]+:)? permission denied for table unpartitioned"


def test_nosuperuser(create_pg):
    _create_pg = create_pg

    def create_pg(name, **kwargs):
        node = _create_pg(name, **kwargs)
        node.set_timeout(test_timeout_default)
        return node

    publisher = create_pg("publisher", allows_streaming="logical")
    subscriber = create_pg("subscriber")
    connstr = publisher.connstr()
    offset = 0  # subscriber log offset for expect_failure

    def publish_insert(tbl, new_i):
        publisher.sql(f"SET SESSION AUTHORIZATION regress_alice; INSERT INTO {tbl} (i) VALUES ({new_i})")

    def publish_update(tbl, old_i, new_i):
        publisher.sql(
            f"SET SESSION AUTHORIZATION regress_alice; UPDATE {tbl} SET i = {new_i} WHERE i = {old_i}"
        )

    def publish_delete(tbl, old_i):
        publisher.sql(f"SET SESSION AUTHORIZATION regress_alice; DELETE FROM {tbl} WHERE i = {old_i}")

    def expect_replication(tbl, cnt, min_i, max_i, testname):
        publisher.wait_for_catchup("admin_sub")
        assert subscriber.sql(f"SELECT COUNT(i), MIN(i), MAX(i) FROM {tbl}") == (cnt, min_i, max_i), testname

    def expect_failure(tbl, cnt, min_i, max_i, pattern, testname):
        nonlocal offset
        offset = subscriber.wait_for_log(pattern, offset)
        assert subscriber.sql(f"SELECT COUNT(i), MIN(i), MAX(i) FROM {tbl}") == (cnt, min_i, max_i), testname

    def revoke_superuser(role):
        subscriber.sql(f"ALTER ROLE {role} NOSUPERUSER")

    def grant_superuser(role):
        subscriber.sql(f"ALTER ROLE {role} SUPERUSER")

    # Roles and schemas owned/published by regress_alice; partitions are laid
    # out differently on publisher and subscriber.
    remainder_a = {"publisher": 0, "subscriber": 1}
    remainder_b = {"publisher": 1, "subscriber": 0}
    for node in (publisher, subscriber):
        ra = remainder_a[node.name]
        rb = remainder_b[node.name]
        node.sql(
            f"""
            CREATE ROLE regress_admin SUPERUSER LOGIN;
            CREATE ROLE regress_alice NOSUPERUSER LOGIN;
            GRANT CREATE ON DATABASE postgres TO regress_alice;
            GRANT PG_CREATE_SUBSCRIPTION TO regress_alice;
            SET SESSION AUTHORIZATION regress_alice;
            CREATE SCHEMA alice;
            GRANT USAGE ON SCHEMA alice TO regress_admin;
            CREATE TABLE alice.unpartitioned (i INTEGER);
            ALTER TABLE alice.unpartitioned REPLICA IDENTITY FULL;
            GRANT SELECT ON TABLE alice.unpartitioned TO regress_admin;
            CREATE TABLE alice.hashpart (i INTEGER) PARTITION BY HASH (i);
            ALTER TABLE alice.hashpart REPLICA IDENTITY FULL;
            GRANT SELECT ON TABLE alice.hashpart TO regress_admin;
            CREATE TABLE alice.hashpart_a PARTITION OF alice.hashpart
              FOR VALUES WITH (MODULUS 2, REMAINDER {ra});
            ALTER TABLE alice.hashpart_a REPLICA IDENTITY FULL;
            CREATE TABLE alice.hashpart_b PARTITION OF alice.hashpart
              FOR VALUES WITH (MODULUS 2, REMAINDER {rb});
            ALTER TABLE alice.hashpart_b REPLICA IDENTITY FULL;
            """
        )

    publisher.sql(
        "SET SESSION AUTHORIZATION regress_alice; "
        "CREATE PUBLICATION alice FOR TABLE alice.unpartitioned, alice.hashpart "
        "WITH (publish_via_partition_root = true)"
    )
    # CREATE SUBSCRIPTION (creates a slot) cannot run in a transaction block, so
    # SET SESSION AUTHORIZATION must be a separate statement on a held connection.
    admin_conn = subscriber.connect()
    admin_conn.sql("SET SESSION AUTHORIZATION regress_admin")
    admin_conn.sql(
        f"CREATE SUBSCRIPTION admin_sub CONNECTION '{connstr}' PUBLICATION alice "
        "WITH (password_required=false)"
    )
    admin_conn.close()
    subscriber.wait_for_subscription_sync(publisher, "admin_sub")

    # Superuser regress_admin can replicate into the tables.
    publish_insert("alice.unpartitioned", 1)
    publish_insert("alice.unpartitioned", 3)
    publish_insert("alice.unpartitioned", 5)
    publish_update("alice.unpartitioned", 1, 7)
    publish_delete("alice.unpartitioned", 3)
    expect_replication("alice.unpartitioned", 2, 5, 7, "superuser admin replicates into unpartitioned")

    # Revoking superuser breaks replication; restoring it recovers.
    revoke_superuser("regress_admin")
    publish_update("alice.unpartitioned", 5, 9)
    expect_failure("alice.unpartitioned", 2, 5, 7, CANNOT_SET_ROLE, "non-superuser admin fails to replicate update")
    grant_superuser("regress_admin")
    expect_replication("alice.unpartitioned", 2, 7, 9, "admin with restored superuser privilege replicates update")

    # Privileges on the target role suffice for non-superuser replication.
    subscriber.sql("ALTER ROLE regress_admin NOSUPERUSER; GRANT regress_alice TO regress_admin")
    publish_insert("alice.unpartitioned", 11)
    expect_replication("alice.unpartitioned", 3, 7, 11,
                       "nosuperuser admin with privileges on role can replicate INSERT into unpartitioned")
    publish_update("alice.unpartitioned", 7, 13)
    expect_replication("alice.unpartitioned", 3, 9, 13,
                       "nosuperuser admin with privileges on role can replicate UPDATE into unpartitioned")
    publish_delete("alice.unpartitioned", 9)
    expect_replication("alice.unpartitioned", 2, 11, 13,
                       "nosuperuser admin with privileges on role can replicate DELETE into unpartitioned")

    # Partitioning.
    publish_insert("alice.hashpart", 101)
    publish_insert("alice.hashpart", 102)
    publish_insert("alice.hashpart", 103)
    publish_update("alice.hashpart", 102, 120)
    publish_delete("alice.hashpart", 101)
    expect_replication("alice.hashpart", 2, 103, 120,
                       "nosuperuser admin with privileges on role can replicate into hashpart")

    # Forced RLS on the target table makes replication fail.
    subscriber.sql(
        "SET SESSION AUTHORIZATION regress_alice; "
        "ALTER TABLE alice.unpartitioned ENABLE ROW LEVEL SECURITY; "
        "ALTER TABLE alice.unpartitioned FORCE ROW LEVEL SECURITY"
    )
    publish_insert("alice.unpartitioned", 15)
    expect_failure("alice.unpartitioned", 2, 11, 13, RLS_FORCED,
                   "replication of insert into table with forced rls fails")

    # Replication acts as the table owner, so it works when RLS is not forced.
    subscriber.sql("ALTER TABLE alice.unpartitioned NO FORCE ROW LEVEL SECURITY")
    expect_replication("alice.unpartitioned", 3, 11, 15, "non-superuser admin can replicate insert if rls is not forced")

    subscriber.sql("ALTER TABLE alice.unpartitioned FORCE ROW LEVEL SECURITY")
    publish_update("alice.unpartitioned", 11, 17)
    expect_failure("alice.unpartitioned", 3, 11, 15, RLS_FORCED,
                   "replication of update into table with forced rls fails")
    subscriber.sql("ALTER TABLE alice.unpartitioned NO FORCE ROW LEVEL SECURITY")
    expect_replication("alice.unpartitioned", 3, 13, 17, "non-superuser admin can replicate update if rls is not forced")

    # Revoking alice's privileges on her own table breaks replication.
    subscriber.sql("REVOKE SELECT, INSERT ON alice.unpartitioned FROM regress_alice")
    publish_insert("alice.unpartitioned", 19)
    expect_failure("alice.unpartitioned", 3, 13, 17, PERM_DENIED,
                   "replication of insert fails if table owner lacks insert permission")

    # INSERT (not SELECT) suffices to replicate an INSERT.
    subscriber.sql("GRANT INSERT ON alice.unpartitioned TO regress_alice")
    expect_replication("alice.unpartitioned", 4, 13, 19, "restoring insert permission permits replication to continue")

    # UPDATE/DELETE need the corresponding permission.
    subscriber.sql("REVOKE UPDATE, DELETE ON alice.unpartitioned FROM regress_alice")
    publish_update("alice.unpartitioned", 13, 21)
    publish_delete("alice.unpartitioned", 15)
    expect_failure("alice.unpartitioned", 4, 13, 19, PERM_DENIED,
                   "replication of update/delete fails if table owner lacks corresponding permission")

    # Restoring UPDATE/DELETE is not enough without SELECT.
    subscriber.sql("GRANT UPDATE, DELETE ON alice.unpartitioned TO regress_alice")
    expect_failure("alice.unpartitioned", 4, 13, 19, PERM_DENIED,
                   "replication of update/delete fails if table owner lacks SELECT permission")

    subscriber.sql("GRANT SELECT ON alice.unpartitioned TO regress_alice")
    expect_replication("alice.unpartitioned", 3, 17, 21, "restoring SELECT permission permits replication to continue")

    # The apply worker restarts when the subscription owner's superuser is revoked.
    grant_superuser("regress_alice")
    alice_conn = subscriber.connect()
    alice_conn.sql("SET SESSION AUTHORIZATION regress_alice")
    alice_conn.sql(f"CREATE SUBSCRIPTION regression_sub CONNECTION '{connstr}' PUBLICATION alice")
    alice_conn.close()
    subscriber.wait_for_subscription_sync(publisher, "regression_sub")

    offset = subscriber.current_log_position()
    revoke_superuser("regress_alice")
    subscriber.wait_for_log(
        r'(?i)LOG: ( [A-Z0-9]+:)? logical replication worker for subscription "regression_sub" '
        r"will restart because the subscription owner's superuser privileges have been revoked",
        offset,
    )

    # A non-superuser subscription owner must supply a password in the connstr.
    publisher1 = create_pg("publisher1", allows_streaming="logical")
    subscriber1 = create_pg("subscriber1")
    pconnstr1 = f"{publisher1.connstr()} user=regress_test_user"
    pconnstr2 = f"{pconnstr1} password=secret"

    for node in (publisher1, subscriber1):
        node.sql(
            """
            CREATE ROLE regress_test_user PASSWORD 'secret' LOGIN REPLICATION;
            GRANT CREATE ON DATABASE postgres TO regress_test_user;
            GRANT PG_CREATE_SUBSCRIPTION TO regress_test_user;
            """
        )
    publisher1.sql("SET SESSION AUTHORIZATION regress_test_user; CREATE PUBLICATION regress_test_pub")
    subscriber1.sql(
        f"CREATE SUBSCRIPTION regress_test_sub CONNECTION '{pconnstr1}' PUBLICATION regress_test_pub"
    )
    subscriber1.wait_for_subscription_sync(publisher1, "regress_test_sub")

    # Require md5 for regress_test_user's local connections to the publisher.
    (publisher1.datadir / "pg_hba.conf").write_text("local all regress_test_user md5\n")
    publisher1.pg_ctl("reload")

    subscriber1.sql("ALTER SUBSCRIPTION regress_test_sub OWNER TO regress_test_user")

    # Without a password in the connection string, REFRESH fails.
    conn = subscriber1.connect()
    conn.sql("SET SESSION AUTHORIZATION regress_test_user")
    with pytest.raises(LibpqError) as excinfo:
        conn.sql("ALTER SUBSCRIPTION regress_test_sub REFRESH PUBLICATION")
    assert "Non-superusers must provide a password in the connection string." in (
        excinfo.value.detail or str(excinfo.value)
    ), "subscription whose owner is a non-superuser must specify password parameter of the connection string"
    conn.close()

    # With the password in the connection string, REFRESH succeeds.
    conn = subscriber1.connect()
    conn.sql("SET SESSION AUTHORIZATION regress_test_user")
    conn.sql(f"ALTER SUBSCRIPTION regress_test_sub CONNECTION '{pconnstr2}'")
    conn.sql("ALTER SUBSCRIPTION regress_test_sub REFRESH PUBLICATION")
    conn.close()
