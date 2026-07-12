# Copyright (c) 2025, PostgreSQL Global Development Group

from __future__ import annotations

import functools
import logging
import os

import pytest

from .paths import INCLUDEDIR_SERVER, SHAREDIR

logger = logging.getLogger(__name__)


# libpq reads many PG* environment variables as connection defaults. A stray
# value would override the connection parameters the framework sets explicitly
# -- most notably the GitHub-hosted Windows runners preset PGUSER=postgres (and
# friends) for their bundled PostgreSQL service, which made every connection try
# to log in as "postgres", a role the test clusters do not have. Clear them up
# front, mirroring what PostgreSQL::Test::Utils does for the Perl TAP tests.
_LIBPQ_ENV_VARS = (
    "PGAPPNAME",
    "PGCLIENTENCODING",
    "PGCONNECT_TIMEOUT",
    "PGDATA",
    "PGDATABASE",
    "PGGSSENCMODE",
    "PGHOST",
    "PGHOSTADDR",
    "PGOPTIONS",
    "PGPASSFILE",
    "PGPASSWORD",
    "PGPORT",
    "PGREQUIREPEER",
    "PGREQUIRESSL",
    "PGSERVICE",
    "PGSERVICEFILE",
    "PGSSLCERT",
    "PGSSLCRL",
    "PGSSLCRLDIR",
    "PGSSLKEY",
    "PGSSLMODE",
    "PGSSLROOTCERT",
    "PGTARGETSESSIONATTRS",
    "PGUSER",
)


def clean_libpq_environment() -> None:
    """Remove inherited libpq connection environment variables (see above)."""
    for var in _LIBPQ_ENV_VARS:
        os.environ.pop(var, None)


def _test_extra_skip_reason(*keys: str) -> str:
    return "requires {} to be set in PG_TEST_EXTRA".format(", ".join(keys))


def _has_test_extra(key: str) -> bool:
    """
    Returns True if the PG_TEST_EXTRA environment variable contains the given
    key.
    """
    extra = os.getenv("PG_TEST_EXTRA", "")
    return key in extra.split()


def require_test_extras(*keys: str) -> pytest.MarkDecorator:
    """
    A convenience annotation which will skip tests if all of the required keys
    are not present in PG_TEST_EXTRA.

    To skip a particular test function or class:

        @pypg.require_test_extras("ldap")
        def test_some_ldap_feature():
            ...

    To skip an entire module:

        pytestmark = pypg.require_test_extra("ssl", "kerberos")
    """
    return pytest.mark.skipif(
        not all([_has_test_extra(k) for k in keys]),
        reason=_test_extra_skip_reason(*keys),
    )


def skip_unless_test_extras(*keys: str) -> None:
    """
    Skip the current test/fixture if any of the required keys are not present
    in PG_TEST_EXTRA. Use this inside fixtures where decorators can't be used.

        @pytest.fixture
        def my_fixture():
            skip_unless_test_extras("ldap")
            ...
    """
    if not all([_has_test_extra(k) for k in keys]):
        pytest.skip(_test_extra_skip_reason(*keys))


_INJECTION_POINTS_SKIP_REASON = "injection points not supported by this build"


@functools.cache
def _injection_points_supported() -> bool:
    """Return whether the server build supports injection points.

    The ``injection_points`` test extension is built and installed only when
    the server was configured with injection point support
    (``-Dinjection_points=true`` / ``--enable-injection-points``), so its
    control file is present in the extension directory exactly when the
    feature is available -- the same signal ``pg_available_extensions`` (and
    the Perl tests' ``check_extension``) rely on. We look at the filesystem
    rather than querying ``pg_available_extensions`` so the check needs only
    an install (a ``pg_config``), not a running node, and can therefore be
    used as a collection-time decorator. The control file is preferred over
    the shared library because its name is platform independent.
    """
    return (SHAREDIR / "extension" / "injection_points.control").exists()


def require_injection_points() -> pytest.MarkDecorator:
    """Skip the decorated test/class/module unless the build supports
    injection points.

        @pypg.require_injection_points()
        def test_some_injection_point():
            ...

    or, for an entire module::

        pytestmark = pypg.require_injection_points()
    """
    return pytest.mark.skipif(
        not _injection_points_supported(),
        reason=_INJECTION_POINTS_SKIP_REASON,
    )


def skip_unless_injection_points() -> None:
    """Skip the current test/fixture unless the build supports injection
    points. Use this inside fixtures where decorators can't be used; prefer
    the ``require_injection_points()`` decorator otherwise.
    """
    if not _injection_points_supported():
        pytest.skip(_INJECTION_POINTS_SKIP_REASON)


@functools.cache
def _pg_config_h_lines() -> tuple[str, ...]:
    """Return the lines of the server build's ``pg_config.h``, stripped.

    Read once and cached, since the build under test does not change during a
    session.
    """
    path = INCLUDEDIR_SERVER / "pg_config.h"
    return tuple(line.strip() for line in path.read_text().splitlines())


def check_pg_config(line: str) -> bool:
    """Return whether the server build's ``pg_config.h`` contains a line that
    starts with ``line``.

    Use it to gate tests on build-time feature macros (mirroring the Perl tests'
    ``check_pg_config``), e.g.::

        if not check_pg_config("#define USE_ICU 1"):
            pytest.skip("ICU not supported by this build")
    """
    return any(candidate.startswith(line) for candidate in _pg_config_h_lines())


def test_timeout_default() -> int:
    """
    Returns the value of the PG_TEST_TIMEOUT_DEFAULT environment variable, in
    seconds, or 180 if one was not provided.
    """
    default = os.getenv("PG_TEST_TIMEOUT_DEFAULT", "")
    if not default:
        return 180

    try:
        return int(default)
    except ValueError as v:
        logger.warning("PG_TEST_TIMEOUT_DEFAULT could not be parsed: " + str(v))
        return 180
