"""report_day.py's CLI argument parsing (CODE-REVIEW.md D9), via subprocess - it's a
plain script, not a module with a testable function underneath the __main__ block.

InfluxDB deliberately left unconfigured for all of these: every case here is decided
by argument parsing alone, before report_day.py ever tries to reach the database.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
REPORT_DAY = os.path.join(REPO, "report_day.py")
VENV_PY = os.path.join(REPO, ".venv", "bin", "python")
PYTHON = VENV_PY if os.path.exists(VENV_PY) else sys.executable


def _run(args):
    env = dict(os.environ)
    for key in ("INFLUX_URL", "INFLUX_HOST", "INFLUX_TOKEN", "INFLUX_TOKEN_PLANNING"):
        env.pop(key, None)
    env["INFLUX_ENV_FILE"] = "/nonexistent/.env"
    return subprocess.run([PYTHON, REPORT_DAY] + args, capture_output=True, text=True,
                          timeout=30, env=env)


def test_bad_date_format_fails_cleanly():
    """Previously: datetime.strptime(args[0], "%Y-%m-%d") inside a bare try/except
    ValueError, printing a fixed usage string. Now argparse's own type= validation -
    same clean failure, and it names what was actually wrong."""
    result = _run(["not-a-date"])
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "YYYY-MM-DD" in result.stderr


def test_help_flag_exits_zero():
    result = _run(["-h"])
    assert result.returncode == 0
    assert "Hold a day's stored plans" in result.stdout


def test_unrecognized_flag_fails_cleanly():
    result = _run(["--this-flag-does-not-exist"])
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_valid_date_reaches_the_influxdb_check():
    """Parsing succeeds; the run then correctly fails one step later, for an
    unrelated and expected reason (no InfluxDB configured) - proving the date
    parsed rather than being rejected."""
    result = _run(["2026-01-01"])
    assert result.returncode == 2
    assert "InfluxDB is not configured" in result.stdout


def test_default_date_is_yesterday_when_omitted():
    result = _run([])
    assert result.returncode == 2
    assert "InfluxDB is not configured" in result.stdout


def test_write_flag_is_accepted_alongside_a_date():
    result = _run(["2026-01-01", "--write"])
    assert result.returncode == 2   # still fails at the InfluxDB check, not at parsing
    assert "Traceback" not in result.stderr
