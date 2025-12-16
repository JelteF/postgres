# Copyright (c) 2025, PostgreSQL Global Development Group

import platform
import shlex
import subprocess
import sys
import time


def shell_path(path):
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


def wait_until(error_message="Did not complete", timeout=5, interval=0.1):
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
