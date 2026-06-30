# Copyright (c) 2025, PostgreSQL Global Development Group

from __future__ import annotations

import os
import platform
import shlex
import subprocess
import sys
import time
from collections.abc import Iterator
from typing import Any


def shell_path(path: str | os.PathLike[str]) -> str:
    """Render ``path`` for embedding in a shell command that the *server* runs
    (archive_command, restore_command, basebackup_to_shell's redirect target,
    ...). This needs to use backslashes on Windows, even in MinGW environments.

    The conversion is a plain ``str.replace`` rather than
    ``pathlib.PureWindowsPath`` because the MinGW (MSYS2) Python swaps
    pathlib's ``_WindowsFlavour`` separator to "/" (and sets ``os.sep = "/"``),
    so ``PureWindowsPath``/``os.path`` still emit forward slashes there even
    though ``platform.system() == "Windows"`` (Python bpo-44778, closed
    "third party"; MinGW-w64 bug https://sourceforge.net/p/mingw-w64/bugs/912/).
    A literal replace is separator-agnostic: on native Windows
    ``str(path)`` is already backslashed (no-op), on MinGW it is forward-slashed
    (converted), and on Unix this branch is skipped.
    """
    if platform.system() == "Windows":
        return str(path).replace("/", "\\")
    return str(path)


def eprint(*args: object, **kwargs: Any) -> None:
    """eprint prints to stderr"""
    print(*args, file=sys.stderr, **kwargs)


def run(
    *command: object,
    check: bool = True,
    shell: bool | None = None,
    silent: bool = False,
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    """run runs the given command and prints it to stderr"""

    __tracebackhide__ = True  # Don't show in pytest stack traces

    if shell is None:
        shell = len(command) == 1 and isinstance(command[0], str)

    # A shell command is a single string; everything else is a list of
    # stringified argv elements. Build it into a fresh local rather than
    # rebinding the *command parameter (whose static type is a tuple).
    cmd: str | list[str]
    if shell:
        # The shell auto-detection above only sets shell when the single
        # argument is a str; an explicit shell=True is the caller's promise of
        # the same, so command[0] is the shell command line.
        assert isinstance(command[0], str)
        cmd = command[0]
    else:
        cmd = [str(c) for c in command]

    if not silent:
        if shell:
            eprint(f"+ {cmd}")
        else:
            eprint(f"+ {shlex.join(cmd)}")

    if silent:
        kwargs.setdefault("stdout", subprocess.DEVNULL)

    result = subprocess.run(cmd, check=False, shell=shell, **kwargs)

    # Manually throw CalledProcessError to avoid subprocess.run's huge body
    # poluting stack traces.
    if check and result.returncode:
        raise subprocess.CalledProcessError(
            result.returncode, cmd, result.stdout, result.stderr
        )

    return result


def capture(
    command: object,
    *args: object,
    stdout: int = subprocess.PIPE,
    encoding: str = "utf-8",
    **kwargs: Any,
) -> str:
    __tracebackhide__ = True  # Don't pollute pytest stack traces

    return run(
        command, *args, stdout=stdout, encoding=encoding, **kwargs
    ).stdout.removesuffix("\n")


def wait_until(
    error_message: str = "Did not complete",
    timeout: float = 5,
    interval: float = 0.1,
) -> Iterator[None]:
    """
    Loop until the timeout is reached. If the timeout is reached, raise an
    exception with the given error message.

    Use it to poll for a condition, breaking out once it holds::

        for _ in wait_until("standby did not catch up", timeout=60):
            if standby.sql("SELECT ...") == expected:
                break
    """
    start = time.time()
    end = start + timeout
    last_printed_progress = start
    while time.time() < end:
        if timeout > 5 and time.time() - last_printed_progress > 5:
            last_printed_progress = time.time()
            print(f"{error_message} in {time.time() - start} seconds - will retry")
        yield
        time.sleep(interval)

    raise TimeoutError(error_message + " in time")
