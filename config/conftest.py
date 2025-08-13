# Copyright (c) 2025, PostgreSQL Global Development Group
#
# Support for check_pytest.py. The configure script provides the path to
# pytest-requirements.txt via the --requirements option added here.

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--requirements",
        help="path to pytest-requirements.txt",
    )


@pytest.fixture
def requirements_file(request):
    return request.config.getoption("--requirements")
