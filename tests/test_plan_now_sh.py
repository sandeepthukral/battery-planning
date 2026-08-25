"""plan-now.sh's guard around the mv into plans/ (CODE-REVIEW.md E3).

Run for real, with BT_DATA_DIR pointed at a tmp_path (plan-now.sh already supports
this - see its own comment on splitting code location from output location) and PY
replaced with a stub that stands in for planner.py.
"""
import os
import stat
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PLAN_NOW_SH = os.path.join(REPO, "plan-now.sh")


def _writeStubPy(binDir, name, script):
    path = os.path.join(binDir, name)
    with open(path, "w") as f:
        f.write("#!/bin/sh\n" + script + "\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _run(dataDir, pyStub, env_extra=None):
    env = dict(os.environ)
    env["BT_DATA_DIR"] = str(dataDir)
    env["PY"] = pyStub
    env["TZ"] = "Europe/Amsterdam"
    env["BT_TZ"] = "Europe/Amsterdam"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(["sh", PLAN_NOW_SH], cwd=str(dataDir), env=env,
                          capture_output=True, text=True, timeout=30)


def test_missing_output_file_fails_clearly_instead_of_a_bare_mv_error(tmp_path):
    """CODE-REVIEW.md E3 / the C7 midnight race: the planner can exit 0 without
    having written the file plan-now.sh expects under today's date (the historical
    branch writes a DIFFERENT date's filename). The old code went straight to `mv`,
    which fails with its own stderr message, then a confusing advise.py traceback.
    Must instead name what happened and stop, before ever calling advise.py."""
    # Exits 0 but writes nothing - reproduces a planner run that silently took the
    # wrong branch rather than refusing (the case C7's guard closes off, but this
    # script must not assume that guard is the only thing standing between it and
    # a missing file).
    stub = _writeStubPy(tmp_path, "fake_python", "exit 0")
    result = _run(tmp_path, stub)

    assert result.returncode == 1
    assert "ERROR: expected planner output" in result.stderr
    assert "advise.py" not in result.stdout + result.stderr   # never reached that far
    plansDir = tmp_path / "plans"
    assert not plansDir.exists() or not list(plansDir.glob("*.txt"))


def test_healthy_run_still_moves_the_output_file(tmp_path):
    """The guard must not get in the way of the ordinary, working case. Writes 13
    hourly rows (not just a header) so the plan also clears advise.py's own
    --min-hours 12 guard downstream - a header-only file would now correctly be
    rejected by the C1a fix, which is not what this test is checking."""
    stub = _writeStubPy(tmp_path, "fake_python", '''
d=$(date +%Y-%m-%d)
today=$(date +%Y%m%d)
out="entsoe-output${today}.txt"
echo "date        time   pvD   pvI   use  nett chrgD  chrg dschg   soc   imp   exp  pr-buy pr-sell    cost" > "$out"
for h in $(seq -w 0 12); do
  echo "$d $h:00     0     0    75    75     0     0     0 10000     0     0 +0.200000 +0.200000 -0.015000" >> "$out"
done
exit 0
''')
    result = _run(tmp_path, stub)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "ERROR: expected planner output" not in result.stdout
    plans = list((tmp_path / "plans").glob("*.txt"))
    assert plans, "expected a plan file to have been written"
