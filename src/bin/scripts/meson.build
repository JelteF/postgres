# Copyright (c) 2022-2026, PostgreSQL Global Development Group

scripts_common = static_library('libscripts_common',
  files('common.c'),
  dependencies: [frontend_code, libpq],
  kwargs: internal_lib_args,
)

binaries = [
  'createdb',
  'dropdb',
  'createuser',
  'dropuser',
  'clusterdb',
  'reindexdb',
  'pg_isready',
]

foreach binary : binaries
  binary_sources = files('@0@.c'.format(binary))

  if host_system == 'windows'
    binary_sources += rc_bin_gen.process(win32ver_rc, extra_args: [
      '--NAME', binary,
      '--FILEDESC', '@0@ - PostgreSQL utility'.format(binary),])
  endif

  binary = executable(binary,
    binary_sources,
    link_with: [scripts_common],
    dependencies: [frontend_code, libpq],
    kwargs: default_bin_args,
  )
  bin_targets += binary
endforeach

vacuuming_common = static_library('libvacuuming_common',
  files('common.c', 'vacuuming.c'),
  dependencies: [frontend_code, libpq],
  kwargs: internal_lib_args,
)

binaries = [
  'vacuumdb',
]
foreach binary : binaries
  binary_sources = files('@0@.c'.format(binary))

  if host_system == 'windows'
    binary_sources += rc_bin_gen.process(win32ver_rc, extra_args: [
      '--NAME', binary,
      '--FILEDESC', '@0@ - PostgreSQL utility'.format(binary),])
  endif

  binary = executable(binary,
    binary_sources,
    link_with: [vacuuming_common],
    dependencies: [frontend_code, libpq],
    kwargs: default_bin_args,
  )
  bin_targets += binary
endforeach

tests += {
  'name': 'scripts',
  'sd': meson.current_source_dir(),
  'bd': meson.current_build_dir(),
  'tap': {
    'env': {'with_icu': icu.found() ? 'yes' : 'no'},
    'tests': [
      't/091_reindexdb_all.pl',
      't/100_vacuumdb.pl',
      't/101_vacuumdb_all.pl',
      't/102_vacuumdb_stages.pl',
      't/200_connstr.pl',
    ],
  },
  'pytest': {
    'env': {'with_icu': icu.found() ? 'yes' : 'no'},
    'tests': [
      'pyt/test_010_clusterdb.py',
      'pyt/test_011_clusterdb_all.py',
      'pyt/test_020_createdb.py',
      'pyt/test_040_createuser.py',
      'pyt/test_050_dropdb.py',
      'pyt/test_070_dropuser.py',
      'pyt/test_080_pg_isready.py',
      'pyt/test_090_reindexdb.py',
    ],
  },
}

subdir('po', if_found: libintl)
