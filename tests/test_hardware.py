"""hardware.py is the single source for battery capacity (CODE-REVIEW.md D4).

Before this, 27900 was written down separately in Marstek-planning.py, advise.py and
report_day.py. This test is the actual point of the fix: it fails if any of the three
ever drifts from hardware.py again, which a grep for "27900" would not catch after a
capacity change edited two of the three but missed one.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
VENV_PY = os.path.join(REPO, ".venv", "bin", "python")
PYTHON = VENV_PY if os.path.exists(VENV_PY) else sys.executable

sys.path.insert(0, REPO)
import hardware


def test_capacity_is_a_plausible_battery_size():
    # Loose sanity bound, not a pin - this is what stops hardware.py itself silently
    # regressing to something nonsensical, independent of what the other files read.
    assert 1000 <= hardware.CAPACITY_WH <= 100000


def test_advise_py_reads_the_shared_constant():
    result = subprocess.run(
        [PYTHON, "-c", "import sys; sys.path.insert(0, %r); import advise; print(advise.CAPACITY_WH)" % REPO],
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert float(result.stdout.strip()) == hardware.CAPACITY_WH


def test_report_day_py_default_matches_the_shared_constant():
    env = dict(os.environ)
    env.pop("BT_CAP", None)   # the default path, not an override
    result = subprocess.run(
        [PYTHON, "-c", "import sys; sys.path.insert(0, %r); import report_day; print(report_day.CAPACITY_WH)" % REPO],
        capture_output=True, text=True, timeout=10, env=env)
    assert result.returncode == 0, result.stderr
    assert float(result.stdout.strip()) == hardware.CAPACITY_WH


def test_report_day_py_bt_cap_still_overrides():
    """The env override (used by backtests/what-if runs) must still work - D4 changed
    where the DEFAULT comes from, not the override mechanism."""
    env = dict(os.environ)
    env["BT_CAP"] = "12345"
    result = subprocess.run(
        [PYTHON, "-c", "import sys; sys.path.insert(0, %r); import report_day; print(report_day.CAPACITY_WH)" % REPO],
        capture_output=True, text=True, timeout=10, env=env)
    assert result.returncode == 0, result.stderr
    assert float(result.stdout.strip()) == 12345.0


def test_marstek_planning_reads_the_shared_constant(planner):
    assert planner.ratedBatteryCapacity == hardware.CAPACITY_WH
