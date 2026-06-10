# Copyright (c) 2022-2026, PostgreSQL Global Development Group

tests += {
  'name': 'authentication',
  'sd': meson.current_source_dir(),
  'bd': meson.current_build_dir(),
  'pytest': {
    'tests': [
      'pyt/test_001_password.py',
      'pyt/test_002_saslprep.py',
    ],
  },
  'tap': {
    'env': {
       'enable_injection_points': get_option('injection_points') ? 'yes' : 'no',
    },
    'tests': [
      't/003_peer.pl',
      't/004_file_inclusion.pl',
      't/005_sspi.pl',
      't/006_login_trigger.pl',
      't/007_pre_auth.pl',
    ],
  },
}
