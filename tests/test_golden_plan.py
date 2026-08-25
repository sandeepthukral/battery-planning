"""Golden-file characterisation tests for Marstek-planning.py.

These do not assert the plan is CORRECT. They assert it is UNCHANGED. That is
deliberate: the planner's ~2,200 lines are refactor targets (see CODE-REVIEW.md,
stages 2-3), and the only cheap way to know a refactor hasn't silently changed a
number is to diff its output against a frozen-in-time reference.

Each scenario runs the real CLI entry point (`python3 Marstek-planning.py -s -p -u -b`)
against frozen fixtures - a copied EnergyZero price-cache response (market prices, not
household data) and a synthetic load/PV CSV (invented numbers, not the real backtest
CSV, which is gitignored occupancy data - see .gitignore's "Household load/solar
export" block). Both a fixed historical BT_START/BT_END and a real EnergyZero cache
hit ("isHistorical" in getPricesFromEnergyZero) mean no network call happens; a broken
HTTP(S)_PROXY is set anyway so a code path that starts reaching the network fails fast
and loudly instead of hanging or silently succeeding against a live API.

To add a scenario: write the env dict, run it once with REGENERATE_GOLDEN=1 to capture
the reference file, read the diff to convince yourself it is what you intended, then
commit the golden file alongside the test.

To intentionally update a golden file after a real behaviour change: rerun with
REGENERATE_GOLDEN=1, and note in the commit message which finding or feature caused
the change - a golden file changing with no explanation in the commit is exactly the
silent drift this test exists to catch.
"""
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PLANNER = os.path.join(REPO, "Marstek-planning.py")
FIXTURES = os.path.join(HERE, "fixtures")

# Prefer the checked-in .venv's interpreter (has pulp/requests installed);
# fall back to the current interpreter, which is what CI's installed-requirements
# environment provides.
VENV_PY = os.path.join(REPO, ".venv", "bin", "python")
PYTHON = VENV_PY if os.path.exists(VENV_PY) else sys.executable

BASE_ENV = {
    # A closed local port. If any code path reaches for the network despite the
    # historical/cached inputs below, this turns that into a fast, loud connection
    # failure instead of a hang or (worse) a real HTTP call made by a test.
    "HTTP_PROXY": "http://127.0.0.1:1/",
    "HTTPS_PROXY": "http://127.0.0.1:1/",
    "BT_INITCHARGE": "14000",       # fixed, not "influx" - a test must not touch InfluxDB
    "BT_MINSOC": "10",
    "BT_RTE": "90",
    "BT_ETAX": "0.11085",
    "BT_CAP": "27900",
    "BT_MAXCHG": "4850",
    "BT_MAXDIS": "4700",
    "BT_GRIDMAX": "8050",
    "BT_CYCLECOSTS": "0.0451",
    "BT_XMLAVAIL": "N",
    "BT_OVERWRITE": "Y",
    "BT_STARTHOUR": "0",
}

SCENARIOS = [
    # id, extra env, plan-now.sh-style CLI flags, golden filename
    pytest.param(
        "winter_quarter_hour",
        {
            "BACKTEST_CSV": os.path.join(FIXTURES, "backtest_winter.csv"),
            "BT_PRICE_CACHE": os.path.join(FIXTURES, "price_cache"),
            "BT_START": "20260101",
            "BT_END": "20260102",
        },
        ["-s", "-p", "-u", "-b"],
        "golden_winter_quarter_hour.txt",
        id="winter_quarter_hour",
    ),
    pytest.param(
        "winter_hourly",
        {
            "BACKTEST_CSV": os.path.join(FIXTURES, "backtest_winter.csv"),
            "BT_PRICE_CACHE": os.path.join(FIXTURES, "price_cache"),
            "BT_START": "20260101",
            "BT_END": "20260102",
        },
        ["-s", "-p", "-u", "-b", "-h"],
        "golden_winter_hourly.txt",
        id="winter_hourly",
    ),
    pytest.param(
        "summer_quarter_hour",
        {
            "BACKTEST_CSV": os.path.join(FIXTURES, "backtest_summer.csv"),
            "BT_PRICE_CACHE": os.path.join(FIXTURES, "price_cache"),
            "BT_START": "20260727",
            "BT_END": "20260728",
        },
        ["-s", "-p", "-u", "-b"],
        "golden_summer_quarter_hour.txt",
        id="summer_quarter_hour",
    ),
]


def _run(extra_env, flags, cwd):
    env = dict(os.environ)
    # Strip everything the shell might already carry that would change the plan
    # (a developer's INFLUX_* or BT_* export, for instance) - the scenario's own env
    # is the complete, sole source of truth for a golden-file run.
    for key in list(env):
        if key.startswith("BT_") or key.startswith("INFLUX_") or key in (
                "HTTP_PROXY", "HTTPS_PROXY"):
            del env[key]
    env.update(BASE_ENV)
    env.update(extra_env)
    result = subprocess.run(
        [PYTHON, PLANNER] + flags,
        cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=120)
    return result


def _outputFile(cwd, start):
    return os.path.join(cwd, "entsoe-output%s.txt" % start)


@pytest.mark.parametrize("name,extra_env,flags,golden_name", SCENARIOS)
def test_golden_plan(name, extra_env, flags, golden_name, tmp_path):
    result = _run(extra_env, flags, cwd=str(tmp_path))
    assert result.returncode == 0, (
        "planner exited %d for scenario %r\nstdout:\n%s\nstderr:\n%s"
        % (result.returncode, name, result.stdout, result.stderr))

    outPath = _outputFile(str(tmp_path), extra_env["BT_START"])
    assert os.path.exists(outPath), (
        "expected output file missing for scenario %r: %s\nstdout:\n%s"
        % (name, outPath, result.stdout))
    with open(outPath) as f:
        actual = f.read()

    goldenPath = os.path.join(FIXTURES, golden_name)
    if os.environ.get("REGENERATE_GOLDEN"):
        with open(goldenPath, "w") as f:
            f.write(actual)
        pytest.skip("REGENERATE_GOLDEN=1: wrote %s, not compared" % goldenPath)

    assert os.path.exists(goldenPath), (
        "no golden file at %s - run with REGENERATE_GOLDEN=1 once to create it, "
        "then read the diff before committing" % goldenPath)
    with open(goldenPath) as f:
        expected = f.read()

    assert actual == expected, (
        "scenario %r drifted from its golden file (%s).\n"
        "If this is an intended behaviour change, rerun with REGENERATE_GOLDEN=1 "
        "and say why in the commit message. If not, this is the regression the "
        "golden-file test exists to catch." % (name, golden_name))


def test_golden_fixtures_are_not_household_data():
    """Guard against a future scenario accidentally pointing BACKTEST_CSV at the real,
    gitignored backtest CSV instead of a synthetic fixture. household load is occupancy
    data (see .gitignore, and the [[irreplaceable-data]] / privacy notes in
    NAS-DEPLOYMENT-PLAN.md) and must never be a test dependency, committed or not."""
    for scenario in SCENARIOS:
        _, extra_env, _, _ = scenario.values
        csvPath = extra_env["BACKTEST_CSV"]
        assert csvPath.startswith(FIXTURES), (
            "scenario points BACKTEST_CSV outside tests/fixtures/: %s" % csvPath)
        assert os.path.basename(csvPath) != "backtest_input_hourly.csv", (
            "scenario points at the real (gitignored, household) backtest CSV")
