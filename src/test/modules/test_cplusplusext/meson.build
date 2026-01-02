# Copyright (c) 2025-2026, PostgreSQL Global Development Group

# This module tests that PostgreSQL headers compile with a C++ compiler.
# It has no runtime tests - if it compiles, the test passes.

if not add_languages('cpp', required: false, native: false)
  subdir_done()
endif

# MSVC has issues with __restrict in Windows SDK headers when compiling C++.
# Skip this test on MSVC for now.
cpp = meson.get_compiler('cpp')
if cpp.get_id() == 'msvc'
  subdir_done()
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
  kwargs: pg_test_mod_args,
)
