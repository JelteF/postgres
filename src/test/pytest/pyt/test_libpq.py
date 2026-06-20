# Copyright (c) 2025, PostgreSQL Global Development Group

import contextlib
import os
import socket
import struct
import threading
from typing import Callable

import pytest

from libpq import connstr, LibpqError


@pytest.mark.parametrize(
    "opts, expected",
    [
        (dict(), ""),
        (dict(port=5432), "port=5432"),
        (dict(port=5432, dbname="postgres"), "port=5432 dbname=postgres"),
        (dict(host=""), "host=''"),
        (dict(host=" "), r"host=' '"),
        (dict(host="a\tb"), "host='a\tb'"),
        (dict(host="a\nb"), "host='a\nb'"),
        (dict(keyword="'"), r"keyword=\'"),
        (dict(keyword=" \\' "), r"keyword=' \\\' '"),
    ],
)
def test_connstr(opts, expected):
    """Tests the escape behavior for connstr()."""
    assert connstr(opts) == expected


def test_must_connect_errors(connect):
    """Tests that connect() raises LibpqError."""
    with pytest.raises(LibpqError, match="invalid connection option"):
        connect(some_unknown_keyword="whatever")
