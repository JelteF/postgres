# Copyright (c) 2025, PostgreSQL Global Development Group

import logging
import os

import pytest

logger = logging.getLogger(__name__)


def _test_extra_skip_reason(*keys: str) -> str:
    return "requires {} to be set in PG_TEST_EXTRA".format(", ".join(keys))


def _has_test_extra(key: str) -> bool:
    """
    Returns True if the PG_TEST_EXTRA environment variable contains the given
    key.
    """
    extra = os.getenv("PG_TEST_EXTRA", "")
    return key in extra.split()


def require_test_extras(*keys: str):
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


def skip_unless_test_extras(*keys: str):
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


def skip_unless_injection_points(node):
    """Skip the current test unless the server build supports injection points.

    The ``injection_points`` test extension is only built and installed when
    the server was configured with ``--enable-injection-points``
    (``-Dinjection_points``), so its presence in ``pg_available_extensions`` is
    a reliable runtime signal. Mirrors the Perl tests' combined
    ``enable_injection_points`` / ``check_extension`` gate.
    """
    available = node.sql(
        "SELECT count(*) > 0 FROM pg_available_extensions "
        "WHERE name = 'injection_points'"
    )
    if not available:
        pytest.skip("injection points not supported by this build")


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
