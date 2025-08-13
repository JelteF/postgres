# Copyright (c) 2025, PostgreSQL Global Development Group

import pytest


@pytest.fixture
def hey():
    yield
    raise "uh-oh"


def test_something(hey):
    assert 2 == 4


def test_something_else():
    assert 2 == 2
