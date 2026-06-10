# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/subscription/t/014_binary.pl.

Binary-mode logical replication: COPY runs in binary format, apply works in
binary, switching binary on/off via ALTER SUBSCRIPTION, custom types with and
without send/recv functions, and mismatched column types (which fail in binary
mode but sync once binary is turned off).

Columns are selected via ``::text`` so the comparison matches psql's text
output regardless of how the client maps numeric/array types.
"""


def test_binary(create_pg):
    # log_statement=all so the COPY format used during tablesync is logged
    # (the Perl test framework enables this by default).
    publisher = create_pg(
        "publisher", allows_streaming="logical", conf={"log_statement": "all"}
    )
    subscriber = create_pg("subscriber")
    connstr = publisher.connstr()

    ddl = [
        """CREATE TABLE public.test_numerical (
            a INTEGER PRIMARY KEY, b NUMERIC, c FLOAT, d BIGINT)""",
        """CREATE TABLE public.test_arrays (
            a INTEGER[] PRIMARY KEY, b NUMERIC[], c TEXT[])""",
    ]
    publisher.sql_batch(*ddl)
    subscriber.sql_batch(*ddl)

    publisher.sql("CREATE PUBLICATION tpub FOR ALL TABLES")

    publisher.sql_batch(
        "INSERT INTO public.test_numerical (a, b, c, d) VALUES (1, 1.2, 1.3, 10), (2, 2.2, 2.3, 20)",
        """INSERT INTO public.test_arrays (a, b, c) VALUES
            ('{1,2,3}', '{1.1, 1.2, 1.3}', '{"one", "two", "three"}'),
            ('{3,1,2}', '{1.3, 1.1, 1.2}', '{"three", "one", "two"}')""",
    )

    offset = publisher.current_log_position()
    subscriber.sql(
        f"CREATE SUBSCRIPTION tsub CONNECTION '{connstr}' "
        "PUBLICATION tpub WITH (slot_name = tpub_slot, binary = true)"
    )
    # COPY runs in binary format on the publisher.
    publisher.wait_for_log(
        r"LOG: ( [A-Z0-9]+:)? statement: COPY (.+)? TO STDOUT WITH \(FORMAT binary\)",
        offset,
    )
    subscriber.wait_for_subscription_sync(publisher, "tsub")

    def numerical():
        return subscriber.sql(
            "SELECT a::text, b::text, c::text, d::text FROM test_numerical ORDER BY a"
        )

    def arrays():
        return subscriber.sql(
            "SELECT a::text, b::text, c::text FROM test_arrays ORDER BY a"
        )

    assert numerical() == [("1", "1.2", "1.3", "10"), ("2", "2.2", "2.3", "20")], (
        "check synced data on subscriber"
    )
    assert arrays() == [
        ("{1,2,3}", "{1.1,1.2,1.3}", "{one,two,three}"),
        ("{3,1,2}", "{1.3,1.1,1.2}", "{three,one,two}"),
    ], "check synced data on subscriber"

    # Apply works in binary mode.
    publisher.sql_batch(
        """INSERT INTO public.test_arrays (a, b, c) VALUES
            ('{2,1,3}', '{1.2, 1.1, 1.3}', '{"two", "one", "three"}'),
            ('{1,3,2}', '{1.1, 1.3, 1.2}', '{"one", "three", "two"}')""",
        "INSERT INTO public.test_numerical (a, b, c, d) VALUES (3, 3.2, 3.3, 30), (4, 4.2, 4.3, 40)",
    )
    publisher.wait_for_catchup("tsub")
    assert numerical() == [
        ("1", "1.2", "1.3", "10"),
        ("2", "2.2", "2.3", "20"),
        ("3", "3.2", "3.3", "30"),
        ("4", "4.2", "4.3", "40"),
    ], "check replicated data on subscriber"

    publisher.sql_batch(
        "UPDATE public.test_arrays SET b[1] = 42, c = NULL",
        "UPDATE public.test_numerical SET b = 42, c = NULL",
    )
    publisher.wait_for_catchup("tsub")
    assert arrays() == [
        ("{1,2,3}", "{42,1.2,1.3}", None),
        ("{1,3,2}", "{42,1.3,1.2}", None),
        ("{2,1,3}", "{42,1.1,1.3}", None),
        ("{3,1,2}", "{42,1.1,1.2}", None),
    ], "check updated replicated data on subscriber"
    assert numerical() == [
        ("1", "42", None, "10"),
        ("2", "42", None, "20"),
        ("3", "42", None, "30"),
        ("4", "42", None, "40"),
    ], "check updated replicated data on subscriber"

    # Switch to text format and back to binary.
    subscriber.sql("ALTER SUBSCRIPTION tsub SET (binary = false)")
    publisher.sql(
        "INSERT INTO public.test_numerical (a, b, c, d) VALUES (5, 5.2, 5.3, 50)"
    )
    publisher.wait_for_catchup("tsub")
    assert numerical() == [
        ("1", "42", None, "10"),
        ("2", "42", None, "20"),
        ("3", "42", None, "30"),
        ("4", "42", None, "40"),
        ("5", "5.2", "5.3", "50"),
    ], "check replicated data on subscriber"

    subscriber.sql("ALTER SUBSCRIPTION tsub SET (binary = true)")
    publisher.sql(
        "INSERT INTO public.test_arrays (a, b, c) VALUES "
        "('{2,3,1}', '{1.2, 1.3, 1.1}', '{\"two\", \"three\", \"one\"}')"
    )
    publisher.wait_for_catchup("tsub")
    assert arrays() == [
        ("{1,2,3}", "{42,1.2,1.3}", None),
        ("{1,3,2}", "{42,1.3,1.2}", None),
        ("{2,1,3}", "{42,1.1,1.3}", None),
        ("{2,3,1}", "{1.2,1.3,1.1}", "{two,three,one}"),
        ("{3,1,2}", "{42,1.1,1.2}", None),
    ], "check replicated data on subscriber"

    # A custom type without send/recv functions can't replicate in binary.
    custom_ddl = [
        "CREATE TYPE myvarchar",
        """CREATE FUNCTION myvarcharin(cstring, oid, integer) RETURNS myvarchar
            LANGUAGE internal IMMUTABLE PARALLEL SAFE STRICT AS 'varcharin'""",
        """CREATE FUNCTION myvarcharout(myvarchar) RETURNS cstring
            LANGUAGE internal IMMUTABLE PARALLEL SAFE STRICT AS 'varcharout'""",
        "CREATE TYPE myvarchar (input = myvarcharin, output = myvarcharout)",
        "CREATE TABLE public.test_myvarchar (a myvarchar)",
    ]
    publisher.sql_batch(*custom_ddl)
    subscriber.sql_batch(*custom_ddl)
    publisher.sql("INSERT INTO public.test_myvarchar (a) VALUES ('a')")

    offset = subscriber.current_log_position()
    subscriber.sql("ALTER SUBSCRIPTION tsub REFRESH PUBLICATION")
    subscriber.wait_for_log(
        r"ERROR: ( [A-Z0-9]+:)? no binary input function available for type", offset
    )

    recv_ddl = [
        """CREATE FUNCTION myvarcharsend(myvarchar) RETURNS bytea
            LANGUAGE internal STABLE PARALLEL SAFE STRICT AS 'varcharsend'""",
        """CREATE FUNCTION myvarcharrecv(internal, oid, integer) RETURNS myvarchar
            LANGUAGE internal STABLE PARALLEL SAFE STRICT AS 'varcharrecv'""",
        "ALTER TYPE myvarchar SET (send = myvarcharsend, receive = myvarcharrecv)",
    ]
    publisher.sql_batch(*recv_ddl)
    subscriber.sql_batch(*recv_ddl)
    subscriber.wait_for_subscription_sync(publisher, "tsub")
    assert subscriber.sql("SELECT a FROM test_myvarchar") == "a", (
        "check synced data on subscriber with custom type"
    )

    # Mismatched column types fail in binary, sync once binary is off.
    publisher.sql_batch(
        "CREATE TABLE public.test_mismatching_types (a bigint PRIMARY KEY)",
        "INSERT INTO public.test_mismatching_types (a) VALUES (1), (2)",
    )
    offset = subscriber.current_log_position()
    subscriber.sql("CREATE TABLE public.test_mismatching_types (a int PRIMARY KEY)")
    subscriber.sql("ALTER SUBSCRIPTION tsub REFRESH PUBLICATION")
    subscriber.wait_for_log(
        r"ERROR: ( [A-Z0-9]+:)? incorrect binary data format", offset
    )

    offset = publisher.current_log_position()
    subscriber.sql("ALTER SUBSCRIPTION tsub SET (binary = false)")
    publisher.wait_for_log(
        r"LOG: ( [A-Z0-9]+:)? statement: COPY (.+)? TO STDOUT\n", offset
    )
    subscriber.wait_for_subscription_sync(publisher, "tsub")
    assert subscriber.sql("SELECT a FROM test_mismatching_types ORDER BY a") == [
        1,
        2,
    ], "check synced data on subscriber with binary = false"
