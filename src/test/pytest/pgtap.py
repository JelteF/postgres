# Copyright (c) 2025, PostgreSQL Global Development Group

import os
import sys

import pytest

#
# Helpers
#


class TAP:
    """
    A basic API for reporting via the TAP 12 protocol.

    https://testanything.org/tap-specification.html
    """

    def __init__(self):
        self.count = 0

    def expect(self, num: int):
        self.print(f"1..{num}")

    def print(self, *args):
        print(*args, file=sys.__stdout__)

    def ok(self, name: str):
        self.count += 1
        self.print("ok", self.count, "-", name)

    def skip(self, name: str, reason: str):
        self.count += 1
        self.print("ok", self.count, "-", name, "# skip", reason)

    def fail(self, name: str, details: str):
        self.count += 1
        self.print("not ok", self.count, "-", name)

        # mtest has some odd behavior around TAP tests where it won't print
        # diagnostics on failure if they're part of the stdout stream, so we
        # might as well just dump the details directly to stderr instead.
        print(details, file=sys.__stderr__)


tap = TAP()


class TestNotes:
    """
    Annotations for a single test. The existing pytest hooks keep interesting
    information somewhat separated across the different stages
    (setup/test/teardown), so this class is used to correlate them.
    """

    skipped = False
    skip_reason = None

    failed = False
    details = ""


# Register a custom key in the stash dictionary for keeping our TestNotes.
notes_key = pytest.StashKey[TestNotes]()


#
# Hook Implementations
#


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    """
    Hijacks the standard streams as soon as possible during pytest startup. The
    pytest-formatted output gets logged to file instead, and we'll use the
    original sys.__stdout__/__stderr__ streams for the TAP protocol.
    """
    logdir = os.getenv("TESTLOGDIR")
    if not logdir:
        raise RuntimeError("pgtap requires the TESTLOGDIR envvar to be set")

    os.makedirs(logdir)
    logpath = os.path.join(logdir, "pytest.log")
    sys.stdout = sys.stderr = open(logpath, "a", buffering=1)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    """
    Suppresses nonzero exit codes due to failed tests. (In that case, we want
    Meson to report a failure count, not a generic ERROR.)
    """
    if exitstatus == pytest.ExitCode.TESTS_FAILED:
        session.exitstatus = pytest.ExitCode.OK


@pytest.hookimpl
def pytest_collectreport(report):
    # Include collection failures directly in Meson error output.
    if report.failed:
        print(report.longreprtext, file=sys.__stderr__)


@pytest.hookimpl
def pytest_internalerror(excrepr, excinfo):
    # Include internal errors directly in Meson error output.
    print(excrepr, file=sys.__stderr__)


#
# Hook Wrappers
#
# In pytest parlance, a "wrapper" for a hook can inspect and optionally modify
# existing hooks' behavior, but it does not replace the hook chain. This is done
# through a generator-style API which chains the hooks together (see the use of
# `yield`).
#


@pytest.hookimpl(hookwrapper=True)
def pytest_collection(session):
    """Reports the number of gathered tests after collection is finished."""
    result = yield
    tap.expect(session.testscollected)
    return result


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """
    Annotates a test item with our TestNotes and grabs relevant information for
    reporting.

    This is called multiple times per test, so it's not correct to print the TAP
    result here. (A test and its teardown stage can both fail, and we want to
    see the details for both.) We instead combine all the information for use by
    our pytest_runtest_protocol wrapper later on.
    """
    report = yield

    if notes_key not in item.stash:
        item.stash[notes_key] = TestNotes()
    notes = item.stash[notes_key]

    if report.passed:
        pass  # no annotation needed

    elif report.skipped:
        notes.skipped = True
        _, _, notes.skip_reason = report.longrepr

    elif report.failed:
        notes.failed = True

        if not notes.details:
            notes.details += "{:_^72}\n\n".format(f" {report.head_line} ")

        if report.when in ("setup", "teardown"):
            notes.details += "\n{:_^72}\n\n".format(
                f" Error during {report.when} of {report.head_line} "
            )

        notes.details += report.longreprtext + "\n"

        # Include captured stdout/stderr/log in failure output
        for section_name, section_content in report.sections:
            if section_content.strip():
                notes.details += "\n{:-^72}\n".format(f" {section_name} ")
                notes.details += section_content + "\n"

    else:
        raise RuntimeError("pytest_runtest_makereport received unknown test status")

    return report


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    """
    Reports the TAP result for this test item using our gathered TestNotes.
    """
    result = yield

    assert notes_key in item.stash, "pgtap didn't annotate a test item?"
    notes = item.stash[notes_key]

    if notes.failed:
        tap.fail(item.nodeid, notes.details)
    elif notes.skipped:
        tap.skip(item.nodeid, notes.skip_reason)
    else:
        tap.ok(item.nodeid)

    return result
