# Copyright (c) 2026, PostgreSQL Global Development Group

test_checksums_sources = files(
	'test_checksums.c',
)

test_checksums = shared_module('test_checksums',
	test_checksums_sources,
	kwargs: pg_test_mod_args,
)
test_install_libs += test_checksums

test_install_data += files(
	'test_checksums.control',
	'test_checksums--1.0.sql',
)

tests += {
  'name': 'test_checksums',
  'sd': meson.current_source_dir(),
  'bd': meson.current_build_dir(),
  'pytest': {
    'tests': [
      'pyt/test_001_basic.py',
    ],
  },
  'tap': {
    'env': {
       'enable_injection_points': get_option('injection_points') ? 'yes' : 'no',
	},
    'tests': [
      't/002_restarts.pl',
      't/003_standby_restarts.pl',
      't/004_offline.pl',
      't/005_injection.pl',
      't/006_pgbench_single.pl',
      't/007_pgbench_standby.pl',
      't/008_pitr.pl',
      't/009_fpi.pl',
    ],
  },
}
