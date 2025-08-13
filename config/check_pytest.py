# Copyright (c) 2025, PostgreSQL Global Development Group
#
# Verify that pytest-requirements.txt is satisfied. This would probably be
# easier with pip, but requiring pip on build machines is a non-starter for
# many.
#
# This is coded as a pytest suite in order to check the Python distribution in
# use by pytest, as opposed to the Python distribution being linked against
# Postgres. In some setups they are separate.
#
# The design philosophy of this script is to bend over backwards to help people
# figure out what is missing. The target audience for error output is the
# buildfarm operator who just wants to get the tests running, not the test
# developer who presumably already knows how to solve these problems.

import importlib
import sys
from typing import List, Union  # needed for earlier Python versions

# importlib.metadata is part of the standard library from 3.8 onwards. Earlier
# Python versions have an official backport called importlib_metadata, which can
# generally be installed as a separate OS package (python3-importlib-metadata).
# This complication can be removed once we stop supporting Python 3.7.
try:
    from importlib import metadata
except ImportError:
    try:
        import importlib_metadata as metadata
    except ImportError:
        # package_version() will need to fall back. This is unlikely to happen
        # in practice, because pytest 7.x depends on importlib_metadata itself.
        metadata = None


def report(*args):
    """
    Prints a configure-time message to the user. (The configure scripts will
    display these messages and ignore the output from the pytest suite.) This
    assumes --capture=no is in use, to avoid pytest's standard stream capture.
    """
    print(*args, file=sys.stderr)


def package_version(pkg: str) -> Union[str, None]:
    """
    Returns the version of the named package, or None if the package is not
    installed.

    This function prefers to use the distribution package version, if we have
    the necessary prerequisites. Otherwise it will fall back to the __version__
    of the imported module, which aligns with pytest.importorskip().
    """
    if metadata is not None:
        try:
            return metadata.version(pkg)
        except metadata.PackageNotFoundError:
            return None

    # This is an older Python and we don't have importlib_metadata. Fall back to
    # __version__ instead.
    try:
        mod = importlib.import_module(pkg)
    except ModuleNotFoundError:
        return None

    if hasattr(mod, "__version__"):
        return mod.__version__

    # We're out of options. If this turns out to cause problems in practice, we
    # might need to require importlib_metadata on older buildfarm members. But
    # since our top-level requirements list will be small, and this possibility
    # will eventually age out with newer Pythons, don't spend more effort on
    # this case for now.
    report(f"Fix check_pytest.py! {pkg} has no __version__")
    assert False, "internal error in package_version()"


def packaging_check(requirements: List[str]) -> bool:
    """
    Reports the status of each required package to the configure program.
    Returns True if all dependencies were found.
    """
    report()  # an opening newline makes the configure output easier to read

    try:
        # packaging contains the PyPA definitions of requirement specifiers.
        # This is contained in a separate OS package (for example,
        # python3-packaging), but it's extremely likely that the user has it
        # installed already, because modern versions of pytest depend on it too.
        import packaging
        from packaging.requirements import Requirement

    except ImportError as err:
        # We don't even have enough prerequisites to check our prerequisites.
        # Print the import error as-is.
        report(err)
        return False

    # Strip extraneous whitespace, whole-line comments, and empty lines from our
    # specifier list.
    requirements = [r.strip() for r in requirements]
    requirements = [r for r in requirements if r and r[0] != "#"]

    found = True
    for spec in requirements:
        req = Requirement(spec)

        # Skip any packages marked as unneeded for this particular Python env.
        if req.marker and not req.marker.evaluate():
            continue

        # Make sure the package is installed...
        version = package_version(req.name)
        if version is None:
            report(f"package '{req.name}': not installed")
            found = False
            continue

        # ...and that it has a compatible version.
        if not req.specifier.contains(version):
            report(
                "package '{}': has version {}, but '{}' is required".format(
                    req.name, version, req.specifier
                ),
            )
            found = False
            continue

        # Report installed packages too, to mirror check_modules.pl.
        report(f"package '{req.name}': installed (version {version})")

    return found


def test_packages(requirements_file):
    """
    Entry point.
    """
    try:
        with open(requirements_file, "r") as f:
            requirements = f.readlines()

        all_found = packaging_check(requirements)

    except Exception as err:
        # Surface any breakage to the configure script before failing the test.
        report(err)
        raise

    assert all_found, "required packages are missing"
