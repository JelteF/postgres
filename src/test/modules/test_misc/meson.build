# Copyright (c) 2022-2026, PostgreSQL Global Development Group

tests += {
  'name': 'test_misc',
  'sd': meson.current_source_dir(),
  'bd': meson.current_build_dir(),
  'pytest': {
    'tests': [
      'pyt/test_001_constraint_validation.py',
      'pyt/test_004_io_direct.py',
      'pyt/test_002_tablespace.py',
      'pyt/test_003_check_guc.py',
      'pyt/test_007_catcache_inval.py',
      'pyt/test_008_replslot_single_user.py',
      'pyt/test_009_log_temp_files.py',
      'pyt/test_011_lock_stats.py',
      'pyt/test_013_temp_obj_multisession.py',
      'pyt/test_005_timeouts.py',
      'pyt/test_006_signal_autovacuum.py',
    ],
  },
  'tap': {
    'env': {
       'enable_injection_points': get_option('injection_points') ? 'yes' : 'no',
    },
    'tests': [
      't/010_index_concurrently_upsert.pl',
      't/012_ddlutils.pl',
      't/014_log_statement_max_length.pl',
    ],
    # The injection points are cluster-wide, so disable installcheck
    'runningcheck': false,
  },
}
