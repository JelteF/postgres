# Copyright (c) 2026, PostgreSQL Global Development Group

"""Port of src/test/modules/test_misc/t/012_ddlutils.pl.

Tests pg_get_database_ddl(), pg_get_tablespace_ddl() and pg_get_role_ddl().
These live in a TAP test rather than the core regression suite because they
create databases and tablespaces, which are heavyweight operations best run
only once.
"""

import re

import pytest

from libpq import LibpqError


def ddl_filter(text):
    """Strip locale/collation details from DDL output so the result is stable
    across platforms (the C equivalent of the Perl helper)."""
    text = re.sub(
        r"\s*\bLOCALE_PROVIDER\b\s*=\s*(?:'[^']*'|\"[^\"]*\"|\S+)", "", text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s*LC_COLLATE\s*=\s*(['\"])[^'\"]*\1", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*LC_CTYPE\s*=\s*(['\"])[^'\"]*\1", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\s*\S*LOCALE\S*\s*=?\s*(['\"])[^'\"]*\1", "", text, flags=re.IGNORECASE
    )
    text = re.sub(
        r"\s*\S*COLLATION\S*\s*=?\s*(['\"])[^'\"]*\1", "", text, flags=re.IGNORECASE
    )
    return text


def test_ddlutils(create_pg):
    # Force UTC so timestamptz values (e.g. VALID UNTIL) render identically
    # regardless of the host's local timezone.
    node = create_pg("main", conf={"timezone": "UTC"})

    def ddl(query):
        # The pg_get_*_ddl functions are set-returning; psql renders the rows
        # joined by newlines, which is what these substring checks expect.
        result = node.sql(query)
        return "\n".join(result) if isinstance(result, list) else result

    #
    # pg_get_role_ddl
    #
    node.sql("CREATE ROLE regress_role_ddl_test1")
    result = ddl("SELECT * FROM pg_get_role_ddl('regress_role_ddl_test1')")
    assert re.search(r"CREATE ROLE regress_role_ddl_test1 .* NOLOGIN", result)

    node.sql(
        "CREATE ROLE regress_role_ddl_test2 "
        "LOGIN SUPERUSER CREATEDB CREATEROLE CONNECTION LIMIT 5 "
        "VALID UNTIL '2030-12-31 23:59:59+00'"
    )
    result = ddl("SELECT * FROM pg_get_role_ddl('regress_role_ddl_test2')")
    assert "SUPERUSER" in result
    assert "CREATEDB" in result
    assert "CONNECTION LIMIT 5" in result
    assert re.search(r"VALID UNTIL '2030-12-31", result)

    node.sql_batch(
        "ALTER ROLE regress_role_ddl_test1 SET work_mem TO '256MB'",
        "ALTER ROLE regress_role_ddl_test1 SET search_path TO myschema, public",
    )
    result = ddl("SELECT * FROM pg_get_role_ddl('regress_role_ddl_test1')")
    assert "SET work_mem TO '256MB'" in result
    assert "SET search_path TO" in result

    # Role with database-specific configuration (needs a real database).
    node.sql(
        "CREATE DATABASE regression_ddlutils_test "
        "TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'"
    )
    node.sql(
        "ALTER ROLE regress_role_ddl_test2 "
        "IN DATABASE regression_ddlutils_test SET work_mem TO '128MB'"
    )
    result = ddl("SELECT * FROM pg_get_role_ddl('regress_role_ddl_test2')")
    assert "IN DATABASE regression_ddlutils_test SET work_mem TO '128MB'" in result

    # Role name requiring quoting.
    node.sql('CREATE ROLE "regress_role-with-dash"')
    result = ddl("SELECT * FROM pg_get_role_ddl('regress_role-with-dash')")
    assert '"regress_role-with-dash"' in result

    # Pretty-printed output indents attributes.
    result = ddl(
        "SELECT * FROM pg_get_role_ddl('regress_role_ddl_test2', 'pretty', 'true')"
    )
    assert re.search(r"\n\s+SUPERUSER", result)

    # Role with memberships.
    node.sql_batch(
        "CREATE ROLE regress_role_ddl_grantor CREATEROLE",
        "CREATE ROLE regress_role_ddl_group1",
        "CREATE ROLE regress_role_ddl_group2",
        "CREATE ROLE regress_role_ddl_member",
        "GRANT regress_role_ddl_group1 TO regress_role_ddl_grantor WITH ADMIN TRUE",
        "GRANT regress_role_ddl_group2 TO regress_role_ddl_grantor WITH ADMIN TRUE",
        "SET ROLE regress_role_ddl_grantor",
        "GRANT regress_role_ddl_group1 TO regress_role_ddl_member "
        "WITH INHERIT TRUE, SET FALSE",
        "GRANT regress_role_ddl_group2 TO regress_role_ddl_member WITH ADMIN TRUE",
        "RESET ROLE",
    )
    result = ddl("SELECT * FROM pg_get_role_ddl('regress_role_ddl_member')")
    assert "GRANT regress_role_ddl_group1 TO regress_role_ddl_member" in result
    assert "SET FALSE" in result
    assert "ADMIN TRUE" in result

    # Memberships suppressed.
    result = ddl(
        "SELECT * FROM pg_get_role_ddl('regress_role_ddl_member', "
        "'memberships', 'false')"
    )
    assert "GRANT" not in result

    # Non-existent role errors.
    with pytest.raises(LibpqError, match="does not exist"):
        node.sql("SELECT * FROM pg_get_role_ddl(9999999::oid)")

    # NULL input returns no rows.
    assert node.sql("SELECT count(*) FROM pg_get_role_ddl(NULL)") == 0

    # Role DDL denied without pg_authid access.
    node.sql_batch(
        "CREATE ROLE regress_role_ddl_noaccess",
        "REVOKE SELECT ON pg_authid FROM PUBLIC",
    )
    with node.connect() as conn:
        conn.sql("SET ROLE regress_role_ddl_noaccess")
        with pytest.raises(LibpqError, match="permission denied"):
            conn.sql("SELECT * FROM pg_get_role_ddl('regress_role_ddl_test1')")
    node.sql("GRANT SELECT ON pg_authid TO PUBLIC")

    #
    # pg_get_database_ddl
    #
    node.sql_batch(
        "ALTER DATABASE regression_ddlutils_test OWNER TO regress_role_ddl_test2",
        "ALTER DATABASE regression_ddlutils_test CONNECTION LIMIT 123",
        "ALTER DATABASE regression_ddlutils_test SET random_page_cost = 2.0",
        "ALTER ROLE regress_role_ddl_test2 "
        "IN DATABASE regression_ddlutils_test SET random_page_cost = 1.1",
    )

    # Non-existent database errors.
    with pytest.raises(LibpqError, match="does not exist"):
        node.sql("SELECT * FROM pg_get_database_ddl('regression_no_such_db')")

    # NULL input returns no rows.
    assert node.sql("SELECT count(*) FROM pg_get_database_ddl(NULL)") == 0

    # Invalid boolean option errors.
    with pytest.raises(LibpqError, match="invalid value"):
        node.sql(
            "SELECT * FROM pg_get_database_ddl('regression_ddlutils_test', "
            "'owner', 'invalid')"
        )

    # Duplicate option errors.
    with pytest.raises(LibpqError, match="duplicate|specified more than once"):
        node.sql(
            "SELECT * FROM pg_get_database_ddl('regression_ddlutils_test', "
            "'owner', 'false', 'owner', 'true')"
        )

    # Basic output (locale details filtered out).
    result = ddl_filter(
        ddl(
            "SELECT pg_get_database_ddl "
            "FROM pg_get_database_ddl('regression_ddlutils_test')"
        )
    )
    assert "CREATE DATABASE regression_ddlutils_test" in result
    assert "TEMPLATE = template0" in result
    assert "ENCODING = 'UTF8'" in result
    assert "OWNER TO regress_role_ddl_test2" in result
    assert "CONNECTION LIMIT = 123" in result
    assert "SET random_page_cost TO '2.0'" in result

    # Pretty-printed output.
    result = ddl_filter(
        ddl(
            "SELECT pg_get_database_ddl "
            "FROM pg_get_database_ddl('regression_ddlutils_test', "
            "'pretty', 'true', 'tablespace', 'false')"
        )
    )
    assert re.search(r"\n\s+WITH TEMPLATE", result)

    # Database DDL denied without CONNECT.
    node.sql("REVOKE CONNECT ON DATABASE regression_ddlutils_test FROM PUBLIC")
    with node.connect() as conn:
        conn.sql("SET ROLE regress_role_ddl_noaccess")
        with pytest.raises(LibpqError):
            conn.sql("SELECT * FROM pg_get_database_ddl('regression_ddlutils_test')")
    node.sql("GRANT CONNECT ON DATABASE regression_ddlutils_test TO PUBLIC")

    #
    # pg_get_tablespace_ddl
    #
    with pytest.raises(LibpqError, match="does not exist"):
        node.sql("SELECT * FROM pg_get_tablespace_ddl('regress_nonexistent_tblsp')")
    with pytest.raises(LibpqError, match="does not exist"):
        node.sql("SELECT * FROM pg_get_tablespace_ddl(0::oid)")

    # NULL input returns no rows (name and OID variants).
    assert node.sql("SELECT count(*) FROM pg_get_tablespace_ddl(NULL::name)") == 0
    assert node.sql("SELECT count(*) FROM pg_get_tablespace_ddl(NULL::oid)") == 0

    # Tablespace name requiring quoting (in-place tablespace).
    with node.connect() as conn:
        conn.sql("SET allow_in_place_tablespaces = true")
        conn.sql(
            'CREATE TABLESPACE "regress_ tblsp" OWNER regress_role_ddl_test1 '
            "LOCATION ''"
        )
    result = ddl("SELECT * FROM pg_get_tablespace_ddl('regress_ tblsp')")
    assert '"regress_ tblsp"' in result

    # Rename and add options; reuse this tablespace for the remaining tests.
    node.sql_batch(
        'ALTER TABLESPACE "regress_ tblsp" RENAME TO regress_allopt_tblsp',
        "ALTER TABLESPACE regress_allopt_tblsp "
        "SET (seq_page_cost = '1.5', random_page_cost = '1.1234567890', "
        "effective_io_concurrency = '17', maintenance_io_concurrency = '18')",
    )

    result = ddl("SELECT * FROM pg_get_tablespace_ddl('regress_allopt_tblsp')")
    assert "CREATE TABLESPACE regress_allopt_tblsp" in result
    assert "OWNER regress_role_ddl_test1" in result
    assert "seq_page_cost='1.5'" in result

    # Pretty-printed output.
    result = ddl(
        "SELECT * FROM pg_get_tablespace_ddl('regress_allopt_tblsp', 'pretty', 'true')"
    )
    assert re.search(r"\n\s+OWNER", result)

    # Owner suppressed.
    result = ddl(
        "SELECT * FROM pg_get_tablespace_ddl('regress_allopt_tblsp', 'owner', 'false')"
    )
    assert "OWNER" not in result

    # Lookup by OID.
    result = ddl(
        "SELECT pg_get_tablespace_ddl FROM pg_get_tablespace_ddl("
        "(SELECT oid FROM pg_tablespace WHERE spcname = 'regress_allopt_tblsp'))"
    )
    assert "CREATE TABLESPACE regress_allopt_tblsp" in result

    # Tablespace DDL denied without pg_tablespace access.
    node.sql("REVOKE SELECT ON pg_tablespace FROM PUBLIC")
    with node.connect() as conn:
        conn.sql("SET ROLE regress_role_ddl_noaccess")
        with pytest.raises(LibpqError, match="permission denied"):
            conn.sql("SELECT * FROM pg_get_tablespace_ddl('regress_allopt_tblsp')")
    node.sql("GRANT SELECT ON pg_tablespace TO PUBLIC")
