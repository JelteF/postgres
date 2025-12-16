# Copyright (c) 2025, PostgreSQL Global Development Group

import shlex
import subprocess
import sys


def eprint(*args, **kwargs):
    """eprint prints to stderr"""
    print(*args, file=sys.stderr, **kwargs)


def run(*command, check=True, shell=None, silent=False, **kwargs):
    """run runs the given command and prints it to stderr"""

    if shell is None:
        shell = len(command) == 1 and isinstance(command[0], str)

    if shell:
        command = command[0]
    else:
        command = list(map(str, command))

    if not silent:
        if shell:
            eprint(f"+ {command}")
        else:
            # We could normally use shlex.join here, but it's not available in
            # Python 3.6 which we still like to support
            unsafe_string_cmd = " ".join(map(shlex.quote, command))
            eprint(f"+ {unsafe_string_cmd}")

    if silent:
        kwargs.setdefault("stdout", subprocess.DEVNULL)

    return subprocess.run(command, check=check, shell=shell, **kwargs)


def capture(command, *args, stdout=subprocess.PIPE, encoding="utf-8", **kwargs):
    return run(
        command, *args, stdout=stdout, encoding=encoding, **kwargs
    ).stdout.removesuffix("\n")
