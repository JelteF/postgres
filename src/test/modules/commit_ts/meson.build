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
      'pyt/test_002_standby.py',
      'pyt/test_003_standby_2.py',
      'pyt/test_004_restart.py',
    ],
  },
}
