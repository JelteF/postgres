# Copyright (c) 2024-2026, PostgreSQL Global Development Group

test_aio_sources = files(
  'test_aio.c',
)

if host_system == 'windows'
  test_aio_sources += rc_lib_gen.process(win32ver_rc, extra_args: [
    '--NAME', 'test_aio',
    '--FILEDESC', 'test_aio - test code for AIO',])
endif

test_aio = shared_module('test_aio',
  test_aio_sources,
  kwargs: pg_test_mod_args,
)
test_install_libs += test_aio

test_install_data += files(
  'test_aio.control',
  'test_aio--1.0.sql',
)

tests += {
  'name': 'test_aio',
  'sd': meson.current_source_dir(),
  'bd': meson.current_build_dir(),
  'pytest': {
    'tests': [
      'pyt/test_001_aio.py',
      'pyt/test_002_io_workers.py',
      'pyt/test_003_initdb.py',
      'pyt/test_004_read_stream.py',
    ],
  },
}
