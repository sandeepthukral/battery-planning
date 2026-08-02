"""backfill_weather.py and capture_weather.py at the command line, via subprocess.

InfluxDB is deliberately left unconfigured, and no case here reaches the network: every
one is decided by argument parsing or by the configuration check that runs before the
fetch. That ordering is the point of several of these tests - a run with nowhere to write
should fail in a second rather than after downloading a month of weather.

The range arithmetic is tested directly against resolveRange() rather than through a
subprocess, since it is a pure function and does not need a process to prove.
"""
import os
import subprocess
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import backfill_weather as bw

VENV_PY = os.path.join(REPO, ".venv", "bin", "python")
PYTHON = VENV_PY if os.path.exists(VENV_PY) else sys.executable


def _run(script, args):
    env = dict(os.environ)
    for key in ("INFLUX_URL", "INFLUX_HOST", "INFLUX_TOKEN", "INFLUX_TOKEN_PLANNING"):
        env.pop(key, None)
    env["INFLUX_ENV_FILE"] = "/nonexistent/.env"
    return subprocess.run([PYTHON, os.path.join(REPO, script)] + args,
                          capture_output=True, text=True, timeout=30, env=env)


# --- backfill_weather.py --------------------------------------------------------------

def test_help_exits_zero_and_shows_the_docstring():
    result = _run("backfill_weather.py", ["-h"])
    assert result.returncode == 0
    assert "weather_observed" in result.stdout


def test_bad_date_is_rejected_by_name():
    result = _run("backfill_weather.py", ["--from", "17-07-2026"])
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "YYYY-MM-DD" in result.stderr


def test_bad_days_is_rejected():
    result = _run("backfill_weather.py", ["nine"])
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_zero_days_is_rejected():
    """0 would silently resolve to a backwards range rather than to nothing."""
    result = _run("backfill_weather.py", ["0"])
    assert result.returncode == 2


def test_days_and_from_together_are_refused():
    """Both express the same thing, and honouring one while ignoring the other would
    write a different range than the one asked for, silently."""
    result = _run("backfill_weather.py", ["30", "--from", "2026-07-17"])
    assert result.returncode == 2
    assert "not both" in result.stderr


def test_unconfigured_influx_stops_before_the_fetch():
    """Fails in a second, not after downloading a month of weather it cannot store."""
    result = _run("backfill_weather.py", ["3"])
    assert result.returncode == 2
    assert "not configured" in result.stdout
    assert "--dry-run" in result.stdout


def test_unrecognised_flag_fails_cleanly():
    result = _run("backfill_weather.py", ["--nope"])
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


# --- range arithmetic -----------------------------------------------------------------

def test_days_counts_back_inclusively():
    """3 days ending today means today and the two before it, not four dates."""
    ns = bw._parseArgs(["3"])
    start, stop = bw.resolveRange(ns, date(2026, 8, 2))
    assert (start, stop) == (date(2026, 7, 31), date(2026, 8, 2))


def test_default_is_thirty_days():
    start, stop = bw.resolveRange(bw._parseArgs([]), date(2026, 8, 2))
    assert (start, stop) == (date(2026, 7, 4), date(2026, 8, 2))


def test_from_without_to_runs_to_today():
    ns = bw._parseArgs(["--from", "2026-07-17"])
    start, stop = bw.resolveRange(ns, date(2026, 8, 2))
    assert (start, stop) == (date(2026, 7, 17), date(2026, 8, 2))


def test_from_and_to_are_used_verbatim():
    ns = bw._parseArgs(["--from", "2026-07-17", "--to", "2026-07-31"])
    assert bw.resolveRange(ns, date(2026, 8, 2)) == (date(2026, 7, 17), date(2026, 7, 31))


def test_a_backwards_range_is_refused_rather_than_fetched():
    assert bw.main(["--from", "2026-08-02", "--to", "2026-07-17"]) == 2


# --- capture_weather.py ---------------------------------------------------------------

def test_capture_help_exits_zero():
    result = _run("capture_weather.py", ["-h"])
    assert result.returncode == 0
    assert "weather_forecast" in result.stdout


def test_capture_unconfigured_influx_stops_before_the_fetch():
    result = _run("capture_weather.py", [])
    assert result.returncode == 2
    assert "not configured" in result.stdout


def test_capture_rejects_a_horizon_open_meteo_cannot_serve():
    result = _run("capture_weather.py", ["--days", "30"])
    assert result.returncode == 2
    assert "1 and 16" in result.stderr
