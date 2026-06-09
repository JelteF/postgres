# Copyright (c) 2021-2026, PostgreSQL Global Development Group

"""Port of src/bin/pg_resetwal/t/001_basic.pl."""

import os
import re


def _slru_files(datadir, subdir):
    """Hex-named SLRU segment files in datadir/subdir, sorted (get_slru_files)."""
    d = os.path.join(str(datadir), subdir)
    return sorted(f for f in os.listdir(d) if re.search(r"[0-9A-F]+", f))


def test_standard_options(pg_bin):
    pg_bin.check_help("pg_resetwal")
    pg_bin.check_version("pg_resetwal")
    pg_bin.check_bad_option("pg_resetwal")


def test_resetwal(create_pg, pg_bin):
    node = create_pg("resetwal")
    datadir = str(node.datadir)
    # The Perl test never starts the node before resetting; stop the one
    # create_pg started, then arrange for commit timestamps so the control
    # override section below has pg_commit_ts segments to work with.
    node.stop()
    node.append_conf("track_commit_timestamp = on")

    r = pg_bin.run("pg_resetwal", "-n", datadir)
    assert r.returncode == 0, r.stderr
    assert re.search(r"checkpoint", r.stdout)

    # NB: the Perl test also checks recursive 0700/0600 permissions on PGDATA
    # here. The pytest framework writes postgresql.log inside the data
    # directory, so that recursive mode check does not apply cleanly; it is
    # omitted.

    assert pg_bin.run("pg_resetwal", "--pgdata", datadir).returncode == 0
    node.start()
    assert node.sql("SELECT 1;") == 1

    r = pg_bin.run("pg_resetwal", datadir)
    assert r.returncode != 0
    assert re.search(r"lock file .* exists", r.stderr)

    node.stop("immediate")
    r = pg_bin.run("pg_resetwal", datadir)
    assert r.returncode != 0
    assert re.search(r"database server was not shut down cleanly", r.stderr)
    assert pg_bin.run("pg_resetwal", "--force", datadir).returncode == 0
    node.start()
    assert node.sql("SELECT 1;") == 1
    node.stop()

    # command-line handling
    pg_bin.check_all("pg_resetwal", "foo", exit_code=1,
                     stderr=[r"error: could not read permissions of directory"])
    pg_bin.check_all("pg_resetwal", "foo", "bar", exit_code=1,
                     stderr=[r"too many command-line arguments"])
    # pg_resetwal ignores PGDATA and requires the directory as an argument.
    pg_bin.check_all("pg_resetwal", exit_code=1,
                     stderr=[r"no data directory specified"])

    error_cases = [
        (["-c", "foo"], r"error: invalid argument for option -c"),
        (["-c", "10,bar"], r"error: invalid argument for option -c"),
        (["-c", "1,10"], r"greater than"),
        (["-c", "10,1"], r"greater than"),
        (["-e", "foo"], r"error: invalid argument for option -e"),
        (["-e", "-1"], r"error: invalid argument for option -e"),
        (["-l", "foo"], r"error: invalid argument for option -l"),
        (["-m", "foo"], r"error: invalid argument for option -m"),
        (["-m", "10,bar"], r"error: invalid argument for option -m"),
        (["-m", "0,10"], r"must not be 0"),
        (["-m", "10,0"], r"must not be 0"),
        (["-o", "foo"], r"error: invalid argument for option -o"),
        (["-o", "0"], r"must not be 0"),
        (["-O", "foo"], r"error: invalid argument for option -O"),
        (["-O", "-1"], r"error: invalid argument for option -O"),
        (["--wal-segsize", "foo"], r"error: invalid value"),
        (["--wal-segsize", "13"], r"must be a power"),
        (["-u", "foo"], r"error: invalid argument for option -u"),
        (["-u", "1"], r"must be greater than"),
        (["-x", "foo"], r"error: invalid argument for option -x"),
        (["-x", "1"], r"must be greater than"),
        (["-x", "-1"], r"error: invalid argument for option -x"),
        (["-x", "-100"], r"error: invalid argument for option -x"),
        (["-x", "10000000000"], r"error: invalid argument for option -x"),
        (["--char-signedness", "foo"], r"error: invalid argument for option --char-signedness"),
    ]
    for opts, pattern in error_cases:
        # the args that take a data directory pass it last; the no-dir cases
        # above are handled separately, so every entry here gets the datadir.
        r = pg_bin.run("pg_resetwal", *opts, datadir)
        assert r.returncode != 0, f"{opts} unexpectedly succeeded"
        assert re.search(pattern, r.stderr), f"{opts}: {r.stderr}"

    # run with control override options
    out = pg_bin.run("pg_resetwal", "--dry-run", datadir).stdout
    m = re.search(r"^Database block size: *(\d+)$", out, re.M)
    assert m, out
    blcksz = int(m.group(1))

    cmd = ["pg_resetwal", "--pgdata", datadir,
           "--epoch", "1",
           "--next-wal-file", "00000001000000320000004B",
           "--next-oid", "100000",
           "--wal-segsize", "1"]

    files = _slru_files(datadir, "pg_commit_ts")
    cmd += ["--commit-timestamp-ids",
            "%d,%d" % (3 if int(files[0], 16) == 0 else int(files[0], 16), int(files[-1], 16))]

    files = _slru_files(datadir, "pg_multixact/offsets")
    mult = 32 * blcksz // 8
    cmd += ["--multixact-ids",
            "%d,%d" % ((int(files[-1], 16) + 1) * mult,
                       1 if int(files[0], 16) == 0 else int(str(int(files[0]) * mult), 16))]

    files = _slru_files(datadir, "pg_multixact/members")
    mult = 32 * int(blcksz / 20) * 4
    cmd += ["--multixact-offset", str((int(files[-1], 16) + 1) * mult)]

    files = _slru_files(datadir, "pg_xact")
    mult = 32 * blcksz * 4
    cmd += ["--oldest-transaction-id",
            str(3 if int(files[0], 16) == 0 else int(files[0], 16) * mult),
            "--next-transaction-id", str((int(files[-1], 16) + 1) * mult)]

    assert pg_bin.run(*cmd, "--dry-run").returncode == 0, "dry run with overrides"
    assert pg_bin.run(*cmd).returncode == 0, "overrides applied"
    r = pg_bin.run("pg_resetwal", "--dry-run", datadir)
    assert re.search(r"^Latest checkpoint's NextOID: *100000$", r.stdout, re.M), r.stdout

    node.start()
    assert node.sql("SELECT 1;") == 1
    node.stop()
