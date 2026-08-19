# Copyright (c) 2022-2026, PostgreSQL Global Development Group

tests += {
  'name': 'brin',
  'sd': meson.current_source_dir(),
  'bd': meson.current_build_dir(),
  'isolation': {
    'specs': [
      'summarization-and-inprogress-insertion',
    ],
  },
  'pytest': {
    'tests': [
      'pyt/test_01_workitems.py',
      'pyt/test_02_wal_consistency.py',
    ],
  },
}
