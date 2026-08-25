"""advise.py's CLI, end to end via subprocess - it's a plain script, not a module
with a testable function underneath the __main__ block.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
ADVISE = os.path.join(REPO, "advise.py")
VENV_PY = os.path.join(REPO, ".venv", "bin", "python")
PYTHON = VENV_PY if os.path.exists(VENV_PY) else sys.executable


def _run(args):
    return subprocess.run([PYTHON, ADVISE] + args, capture_output=True, text=True, timeout=30)


def test_empty_plan_fails_min_hours_guard(tmp_path):
    """CODE-REVIEW.md C1a: an empty plan file used to `continue` past the --min-hours
    check entirely and exit 0 - exactly the gap that let a starved planning run (see
    LPoptimization()'s C1b refusal) look like a healthy short one to plan-now.sh."""
    empty = tmp_path / "empty_plan.txt"
    empty.write_text("")
    result = _run(["--min-hours", "12", str(empty)])
    assert result.returncode == 1, "stdout:\n%s" % result.stdout
    assert "ERROR" in result.stdout


def test_empty_plan_without_min_hours_still_succeeds(tmp_path):
    """No --min-hours given: an empty plan is just reported, not a failure - the guard
    is opt-in, matching plan-now.sh's own usage."""
    empty = tmp_path / "empty_plan.txt"
    empty.write_text("")
    result = _run([str(empty)])
    assert result.returncode == 0, "stdout:\n%s" % result.stdout


def test_healthy_plan_passes_min_hours_guard(tmp_path):
    plan = tmp_path / "plan.txt"
    plan.write_text(
        "date        time   pvD   pvI   use  nett chrgD  chrg dschg   soc   imp   exp  pr-buy pr-sell    cost\n"
        "2026-01-01 00:00     0     0    75    75     0     0     0 10000     0     0 +0.200000 +0.200000 -0.015000\n"
        "2026-01-01 01:00     0     0    75    75     0     0     0 10000     0     0 +0.200000 +0.200000 -0.015000\n"
    )
    result = _run(["--min-hours", "1", str(plan)])
    assert result.returncode == 0, "stdout:\n%s" % result.stdout


# --- argparse footguns (CODE-REVIEW.md D9) ---------------------------------------------


def test_min_hours_with_no_value_fails_cleanly(tmp_path):
    """Previously: rawArgs.index("--min-hours"); rawArgs[i+1] raised a bare
    IndexError with a traceback if --min-hours was the last argument."""
    plan = tmp_path / "plan.txt"
    plan.write_text("x")
    result = _run(["--min-hours"])
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "error" in result.stderr.lower()


def test_min_hours_with_non_numeric_value_fails_cleanly(tmp_path):
    """Previously: float(rawArgs[i+1]) raised a bare ValueError with a traceback."""
    plan = tmp_path / "plan.txt"
    plan.write_text("x")
    result = _run(["--min-hours", "not-a-number", str(plan)])
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "error" in result.stderr.lower()


def test_no_arguments_prints_docstring_and_exits_2():
    result = _run([])
    assert result.returncode == 2
    assert "Turn a planner.py plan table" in result.stdout
