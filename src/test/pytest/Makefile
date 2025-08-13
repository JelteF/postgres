#-------------------------------------------------------------------------
#
# Makefile for pytest
#
# Portions Copyright (c) 1996-2025, PostgreSQL Global Development Group
# Portions Copyright (c) 1994, Regents of the University of California
#
# src/test/pytest/Makefile
#
#-------------------------------------------------------------------------

subdir = src/test/pytest
top_builddir = ../../..
include $(top_builddir)/src/Makefile.global

check:
	$(pytest_check)

clean distclean maintainer-clean:
	rm -rf tmp_check
