# Copyright (c) 2025, PostgreSQL Global Development Group

from __future__ import annotations

from ._env import (
    check_pg_config,
    clean_libpq_environment,
    require_injection_points,
    require_test_extras,
    skip_unless_injection_points,
    skip_unless_test_extras,
)
from .server import PostgresServer

# Clear inherited libpq connection environment variables as soon as the test
# framework is imported, before any server is started or connection is made.
clean_libpq_environment()

__all__ = [
    "check_pg_config",
    "require_injection_points",
    "require_test_extras",
    "skip_unless_injection_points",
    "skip_unless_test_extras",
    "PostgresServer",
]
