# Copyright (c) 2025-2026, PostgreSQL Global Development Group

# This module tests that PostgreSQL headers compile with a C++ compiler.
# It has no runtime tests - if it compiles, the test passes.

if not add_languages('cpp', required: false, native: false)
  subdir_done()
endif

# MSVC needs C++20 for designated initializers used in PG_MODULE_MAGIC and
# list_make macros. Also suppress C4200 warnings about flexible array members.
cpp = meson.get_compiler('cpp')
if cpp.get_id() == 'msvc'
  test_cplusplusext_cpp_args = ['/std:c++20', '/wd4200']
else
  test_cplusplusext_cpp_args = []
endif

test_cplusplusext_sources = files(
  'test_cplusplusext.cpp',
)

if host_system == 'windows'
  test_cplusplusext_sources += rc_lib_gen.process(win32ver_rc, extra_args: [
    '--NAME', 'test_cplusplusext',
    '--FILEDESC', 'test_cplusplusext - test C++ compatibility of PostgreSQL headers',])
endif

test_cplusplusext = shared_module('test_cplusplusext',
  test_cplusplusext_sources,
  kwargs: pg_test_mod_args + {
    'cpp_args': pg_test_mod_args.get('cpp_args', []) + test_cplusplusext_cpp_args,
  },
)
