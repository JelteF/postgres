# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/test/authentication/t/004_file_inclusion.pl.

Tests ``include``, ``include_if_exists`` and ``include_dir`` directives in the
HBA and ident configuration files, plus ``@file`` token expansion. A tree of
config files is built with these directives while the expected contents of the
``pg_hba_file_rules`` and ``pg_ident_file_mappings`` catalog views are
accumulated in parallel; after a restart the views are queried and compared.

The catalog rows are compared as raw psql ``-tA`` text (matching how the Perl
original built its expected strings), since the array columns render as
``{a,b}`` literals that the object model would not reproduce verbatim.

This test can only run with Unix-domain sockets (the framework already uses
them on non-Windows platforms).
"""

import os


def test_file_inclusion(create_pg, pg_bin):
    node = create_pg("primary")
    data_dir = node.datadir

    # Tracks pg_hba_file_rules.rule_number / pg_ident_file_mappings.map_number
    # (the global priority counters), plus a per-file physical line counter.
    line_counters = {"hba_rule": 0, "ident_rule": 0}

    def append_conf(filename, entry):
        (data_dir / filename).parent.mkdir(parents=True, exist_ok=True)
        with open(data_dir / filename, "a") as f:
            f.write(entry + "\n")

    def add_hba_line(filename, entry):
        """Append an HBA entry and return the pg_hba_file_rules row it produces
        (empty for an include directive, which generates no catalog row)."""
        append_conf(filename, entry)
        base_filename = os.path.basename(filename)
        line_counters[filename] = line_counters.get(filename, 0) + 1
        fileline = line_counters[filename]
        if entry.startswith("include"):
            return ""
        line_counters["hba_rule"] += 1
        globline = line_counters["hba_rule"]
        tokens = entry.split(" ")
        tokens[1] = "{" + tokens[1] + "}"  # database
        tokens[2] = "{" + tokens[2] + "}"  # user_name
        tokens += ["", ""]  # options, error
        line = "\n" if globline > 1 else ""
        line += f"{globline}|{base_filename}|{fileline}|"
        line += "|".join(tokens)
        return line

    def add_ident_line(filename, entry):
        """Append an ident entry and return the pg_ident_file_mappings row it
        produces (empty for an include directive)."""
        append_conf(filename, entry)
        base_filename = os.path.basename(filename)
        line_counters[filename] = line_counters.get(filename, 0) + 1
        fileline = line_counters[filename]
        if entry.startswith("include"):
            return ""
        line_counters["ident_rule"] += 1
        globline = line_counters["ident_rule"]
        tokens = entry.split(" ")
        tokens += [""]  # error
        line = "\n" if globline > 1 else ""
        line += f"{globline}|{base_filename}|{fileline}|"
        line += "|".join(tokens)
        return line

    # Locations for the entry points of the HBA and ident files.
    hba_file = "subdir1/pg_hba_custom.conf"
    ident_file = "subdir2/pg_ident_custom.conf"

    hba_expected = ""
    ident_expected = ""

    # Customise the main auth file names.
    node.sql(f"ALTER SYSTEM SET hba_file = '{data_dir}/{hba_file}'")
    node.sql(f"ALTER SYSTEM SET ident_file = '{data_dir}/{ident_file}'")

    # Remove the original ones, this node links to non-default ones now.
    (data_dir / "pg_hba.conf").unlink()
    (data_dir / "pg_ident.conf").unlink()

    # --- Generate HBA contents with include directives ---

    # First, make sure that we will always be able to connect.
    hba_expected += add_hba_line(hba_file, "local all all trust")

    # "include". Note that as hba_file is located in subdir1, pg_hba_pre.conf
    # is located at the root of the data directory.
    hba_expected += add_hba_line(hba_file, "include ../pg_hba_pre.conf")
    hba_expected += add_hba_line("pg_hba_pre.conf", "local pre all reject")
    hba_expected += add_hba_line(hba_file, "local all all reject")
    add_hba_line(hba_file, "include ../hba_pos/pg_hba_pos.conf")
    hba_expected += add_hba_line("hba_pos/pg_hba_pos.conf", "local pos all reject")
    # When an include directive refers to a relative path, it is compiled from
    # the base location of the file loaded from.
    hba_expected += add_hba_line("hba_pos/pg_hba_pos.conf", "include pg_hba_pos2.conf")
    hba_expected += add_hba_line("hba_pos/pg_hba_pos2.conf", "local pos2 all reject")
    hba_expected += add_hba_line("hba_pos/pg_hba_pos2.conf", "local pos3 all reject")

    # include_if_exists data, nothing generated for the catalog.
    # Missing file, no catalog entries.
    hba_expected += add_hba_line(hba_file, "include_if_exists ../hba_inc_if/none")
    # File with some contents loaded.
    hba_expected += add_hba_line(hba_file, "include_if_exists ../hba_inc_if/some")
    hba_expected += add_hba_line("hba_inc_if/some", "local if_some all reject")

    # include_dir
    hba_expected += add_hba_line(hba_file, "include_dir ../hba_inc")
    hba_expected += add_hba_line("hba_inc/01_z.conf", "local dir_z all reject")
    hba_expected += add_hba_line("hba_inc/02_a.conf", "local dir_a all reject")
    # Garbage file not suffixed by .conf, so it will be ignored.
    append_conf("hba_inc/garbageconf", "should not be included")

    # Authentication file expanded in an existing entry for database names.
    # As it is expanded, ignore the output generated.
    add_hba_line(hba_file, "local @../dbnames.conf all reject")
    append_conf("dbnames.conf", "db1")
    append_conf("dbnames.conf", "db3")
    hba_expected += (
        "\n"
        + str(line_counters["hba_rule"])
        + "|"
        + os.path.basename(hba_file)
        + "|"
        + str(line_counters[hba_file])
        + "|local|{db1,db3}|{all}|reject||"
    )

    # --- Generate ident structure with include directives ---

    # include. Note that pg_ident_pre.conf is located at the root of the data
    # directory.
    ident_expected += add_ident_line(ident_file, "include ../pg_ident_pre.conf")
    ident_expected += add_ident_line("pg_ident_pre.conf", "pre foo bar")
    ident_expected += add_ident_line(ident_file, "test a b")
    ident_expected += add_ident_line(
        ident_file, "include ../ident_pos/pg_ident_pos.conf"
    )
    ident_expected += add_ident_line("ident_pos/pg_ident_pos.conf", "pos foo bar")
    # When an include directive refers to a relative path, it is compiled from
    # the base location of the file loaded from.
    ident_expected += add_ident_line(
        "ident_pos/pg_ident_pos.conf", "include pg_ident_pos2.conf"
    )
    ident_expected += add_ident_line("ident_pos/pg_ident_pos2.conf", "pos2 foo bar")
    ident_expected += add_ident_line("ident_pos/pg_ident_pos2.conf", "pos3 foo bar")

    # include_if_exists
    # Missing file, no catalog entries.
    ident_expected += add_ident_line(
        ident_file, "include_if_exists ../ident_inc_if/none"
    )
    # File with some contents loaded.
    ident_expected += add_ident_line(
        ident_file, "include_if_exists ../ident_inc_if/some"
    )
    ident_expected += add_ident_line("ident_inc_if/some", "if_some foo bar")

    # include_dir
    ident_expected += add_ident_line(ident_file, "include_dir ../ident_inc")
    ident_expected += add_ident_line("ident_inc/01_z.conf", "dir_z foo bar")
    ident_expected += add_ident_line("ident_inc/02_a.conf", "dir_a foo bar")
    # Garbage file not suffixed by .conf, so it will be ignored.
    append_conf("ident_inc/garbageconf", "should not be included")

    node.pg_ctl("restart")

    # The base path is filtered out, keeping only the file name to bypass
    # portability issues. The configuration files had better have unique names.
    def psql_text(sql):
        r = pg_bin.run("psql", "-X", "-A", "-t", "-q", "-c", sql, server=node,
                       check=True)
        return r.stdout.rstrip("\n")

    contents = psql_text(
        "SELECT rule_number,"
        " regexp_replace(file_name, '.*/', ''),"
        " line_number, type, database, user_name, auth_method, options, error"
        " FROM pg_hba_file_rules ORDER BY rule_number;"
    )
    assert contents == hba_expected, "check contents of pg_hba_file_rules"

    contents = psql_text(
        "SELECT map_number,"
        " regexp_replace(file_name, '.*/', ''),"
        " line_number, map_name, sys_name, pg_username, error"
        " FROM pg_ident_file_mappings ORDER BY map_number"
    )
    assert contents == ident_expected, "check contents of pg_ident_file_mappings"
