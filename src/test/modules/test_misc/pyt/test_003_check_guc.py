# Copyright (c) 2024-2026, PostgreSQL Global Development Group

"""Port of src/test/modules/test_misc/t/003_check_guc.pl.

Cross-check the consistency of GUC parameters with postgresql.conf.sample.
"""

import re

from pypg.bins import pg_config


def test_check_guc(conn):
    # All parameters that can be listed in the sample file. config_file is an
    # exception (not in the sample but part of guc_tables.c). Custom GUCs
    # loaded by extensions are excluded.
    all_params = [
        p.lower()
        for p in conn.sql(
            "SELECT name FROM pg_settings"
            " WHERE NOT 'NOT_IN_SAMPLE' = ANY (pg_settings_get_flags(name))"
            "   AND name <> 'config_file' AND category <> 'Customized Options'"
            " ORDER BY 1"
        )
    ]
    not_in_sample = [
        p.lower()
        for p in conn.sql(
            "SELECT name FROM pg_settings"
            " WHERE 'NOT_IN_SAMPLE' = ANY (pg_settings_get_flags(name))"
            " ORDER BY 1"
        )
    ]

    share_dir = pg_config.capture("--sharedir")
    sample_file = f"{share_dir}/postgresql.conf.sample"

    gucs_in_file = []
    lines_with_tabs = []
    with open(sample_file) as f:
        for line_num, line in enumerate(f, start=1):
            if "\t" in line:
                lines_with_tabs.append(line_num)

            # Each parameter is preceded by "#" (not "# ") and followed
            # immediately by " = ".
            m = re.match(r"^#([_a-zA-Z0-9]+) = .*", line)
            if m:
                param_name = m.group(1).lower()
                if param_name in ("include", "include_dir", "include_if_exists"):
                    continue
                gucs_in_file.append(param_name)
                continue
            # Every other line must start with a # or whitespace.
            assert not re.match(r"^\s*[^#\s]", line), (
                f"{line!r} missing initial # in postgresql.conf.sample"
            )

    gucs_in_file_set = set(gucs_in_file)
    all_params_set = set(all_params)
    not_in_sample_set = set(not_in_sample)

    missing_from_file = [p for p in all_params if p not in gucs_in_file_set]
    assert missing_from_file == [], (
        f"GUCs in guc_tables.c missing from postgresql.conf.sample: {missing_from_file}"
    )

    missing_from_list = [p for p in gucs_in_file if p not in all_params_set]
    assert missing_from_list == [], (
        f"GUCs in postgresql.conf.sample with incorrect info in guc_tables.c: {missing_from_list}"
    )

    sample_intersect = [p for p in gucs_in_file if p in not_in_sample_set]
    assert sample_intersect == [], (
        f"GUCs in postgresql.conf.sample marked as NOT_IN_SAMPLE: {sample_intersect}"
    )

    assert lines_with_tabs == [], (
        f"lines with tabs in postgresql.conf.sample: {lines_with_tabs}"
    )
