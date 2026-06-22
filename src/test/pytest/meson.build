# Copyright (c) 2025, PostgreSQL Global Development Group

if not pytest_enabled
  subdir_done()
endif

tests += {
  'name': 'pytest',
  'sd': meson.current_source_dir(),
  'bd': meson.current_build_dir(),
  'pytest': {
    'tests': [
      'pyt/test_background_sql.py',
      'pyt/test_errors.py',
      'pyt/test_libpq.py',
      'pyt/test_multi_server.py',
      'pyt/test_query_helpers.py',
    ],
  },
}
