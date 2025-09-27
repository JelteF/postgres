# Copyright (c) 2025, PostgreSQL Global Development Group

import logging
import os
from typing import List, Optional

import pytest

logger = logging.getLogger(__name__)


def has_test_extra(key: str) -> bool:
    """
    Returns True if the PG_TEST_EXTRA environment variable contains the given
    key.
    """
    extra = os.getenv("PG_TEST_EXTRA", "")
    return key in extra.split()


def require_test_extra(*keys: str) -> bool:
    """
    A convenience annotation which will skip tests if all of the required keys
    are not present in PG_TEST_EXTRA.

    To skip a particular test function or class:

        @pg.require_test_extra("ldap")
        def test_some_ldap_feature():
            ...

    To skip an entire module:

        pytestmark = pg.require_test_extra("ssl", "kerberos")
    """
    return pytest.mark.skipif(
        not all([has_test_extra(k) for k in keys]),
        reason="requires {} to be set in PG_TEST_EXTRA".format(", ".join(keys)),
    )


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
