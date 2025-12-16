# Copyright (c) 2025, PostgreSQL Global Development Group

import shlex
import subprocess
import sys


def eprint(*args, **kwargs):
    """eprint prints to stderr"""
    print(*args, file=sys.stderr, **kwargs)


def run(*command, check=True, shell=None, silent=False, **kwargs):
    """run runs the given command and prints it to stderr"""

    __tracebackhide__ = True  # Don't show in pytest stack traces

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

    result = subprocess.run(command, check=False, shell=shell, **kwargs)

    # Manually throw CalledProcessError to avoid subprocess.run's huge body
    # poluting stack traces.
    if check and result.returncode:
        raise subprocess.CalledProcessError(
            result.returncode, command, result.stdout, result.stderr
        )

    return result


def capture(command, *args, stdout=subprocess.PIPE, encoding="utf-8", **kwargs):
    __tracebackhide__ = True  # Don't pollute pytest stack traces

    return run(
        command, *args, stdout=stdout, encoding=encoding, **kwargs
    ).stdout.removesuffix("\n")
