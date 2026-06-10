# Copyright (c) 2022-2026, PostgreSQL Global Development Group

tests += {
  'name': 'ssl',
  'sd': meson.current_source_dir(),
  'bd': meson.current_build_dir(),
  'pytest': {
    'tests': [
      'pyt/test_002_scram.py',
      'pyt/test_003_sslinfo.py',
      'pyt/test_004_sni.py',
    ],
  },
  'tap': {
    'env': {
      'with_ssl': ssl_library,
      'OPENSSL': openssl.found() ? openssl.full_path() : '',
    },
    'tests': [
      't/001_ssltests.pl',
    ],
  },
}
