# Copyright (c) 2022-2026, PostgreSQL Global Development Group

tests += {
  'name': 'commit_ts',
  'sd': meson.current_source_dir(),
  'bd': meson.current_build_dir(),
  'regress': {
    'sql': [
      'commit_timestamp',
    ],
    # Disabled because these tests require "track_commit_timestamp = on",
    # which typical runningcheck users do not have (e.g. buildfarm clients).
    'runningcheck': false,
  },
  'pytest': {
    'tests': [
      'pyt/test_001_base.py',
    ],
  },
  'tap': {
    'tests': [
      't/002_standby.pl',
      't/003_standby_2.pl',
      't/004_restart.pl',
    ],
  },
}
