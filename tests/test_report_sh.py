"""scripts/report.sh, run for real against a stubbed `docker` on PATH.

REPO_DIR is overridable (see the script) specifically so this can run against a
tmp_path instead of the hardcoded NAS path.
"""
import os
import stat
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
REPORT_SH = os.path.join(REPO, "scripts", "report.sh")

DAY = "2026-07-31"
OUT_NAME = "report_20260731.txt"


def _writeStubDocker(binDir, stdout, exitCode):
    """A fake `docker` that ignores its arguments, prints `stdout`, and exits
    `exitCode` - standing in for `docker compose run --rm --no-deps planner
    python3 /app/report_day.py ...` without needing Docker or the real container.

    Referenced via the script's ${DOCKER:-docker} override, not PATH: report.sh
    itself does `PATH="/usr/local/bin:/usr/bin:/bin:$PATH"`, which would put a real
    installed `docker` ahead of anything a test merely prepends to PATH.
    """
    path = os.path.join(binDir, "docker")
    with open(path, "w") as f:
        f.write("#!/bin/sh\ncat <<'EOF'\n%s\nEOF\nexit %d\n" % (stdout, exitCode))
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _run(repoDir, dockerStub, day=DAY):
    env = dict(os.environ)
    env["REPO_DIR"] = str(repoDir)
    env["TZ"] = "Europe/Amsterdam"
    env["DOCKER"] = dockerStub
    return subprocess.run(["sh", REPORT_SH, day], cwd=str(repoDir), env=env,
                          capture_output=True, text=True, timeout=30)


@pytest.fixture
def repoDir(tmp_path):
    (tmp_path / "data" / "reports").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def binDir(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    return str(d)


def test_successful_run_writes_the_report(repoDir, binDir):
    stub = _writeStubDocker(binDir, "MONEY OUTCOMES FORECAST", exitCode=0)
    result = _run(repoDir, stub)
    assert result.returncode == 0, result.stdout + result.stderr
    out = repoDir / "data" / "reports" / OUT_NAME
    assert out.exists()
    assert "MONEY OUTCOMES FORECAST" in out.read_text()


def test_no_plans_stored_still_writes_the_report(repoDir, binDir):
    """rc 1 means report_day.py ran to completion and said "no plans stored for that
    day" - a real, complete report, not a failure. Must still be moved into place."""
    stub = _writeStubDocker(binDir, "No plans stored for 2026-07-31.", exitCode=1)
    result = _run(repoDir, stub)
    assert result.returncode == 1
    out = repoDir / "data" / "reports" / OUT_NAME
    assert out.exists()
    assert "No plans stored" in out.read_text()


def test_failed_run_does_not_overwrite_yesterdays_good_report(repoDir, binDir):
    """CODE-REVIEW.md E1: the mv used to run unconditionally, so a failed run (a
    docker/InfluxDB error, rc >= 2) would silently replace a genuinely good report
    with today's error message. The good report must survive."""
    out = repoDir / "data" / "reports" / OUT_NAME
    out.write_text("GOOD REPORT FROM A REAL RUN\n")

    stub = _writeStubDocker(binDir, "Traceback (most recent call last): boom", exitCode=2)
    result = _run(repoDir, stub)

    assert result.returncode == 2
    assert out.read_text() == "GOOD REPORT FROM A REAL RUN\n", (
        "the previous good report was overwritten by a failed run")
    # the failed run's own output is kept somewhere, not silently discarded
    leftovers = list((repoDir / "data" / "reports").glob(OUT_NAME + ".*"))
    assert leftovers, "expected the failed run's output to be kept as OUT.$$"
    assert "Traceback" in leftovers[0].read_text()
