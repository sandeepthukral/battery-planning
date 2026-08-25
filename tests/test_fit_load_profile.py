"""fit_load_profile.py: the pure bucketing/aggregation helpers, collect() against a faked
InfluxDB, and the CLI surface via subprocess.

The three layers are tested three different ways on purpose, matching what is already here:
the helpers like test_solar.py (plain data in, arithmetic out), collect() like
test_influx_source.py (monkeypatch the module-level query functions), and the CLI like
test_report_day_cli.py (subprocess with InfluxDB deliberately unconfigured).
"""
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import fit_load_profile as fl
import influx_source as ix

SCRIPT = os.path.join(REPO, "fit_load_profile.py")
VENV_PY = os.path.join(REPO, ".venv", "bin", "python")
PYTHON = VENV_PY if os.path.exists(VENV_PY) else sys.executable

TZ = ix.LOCAL_TZ or timezone.utc
# A Wednesday, so weekday/weekend cases can be built by adding whole days without arithmetic
# in the test itself.
WEDNESDAY = datetime(2026, 7, 29, 0, 0, tzinfo=TZ)


def _row(hour, forecast, actual, dayOffset=0):
    return (WEDNESDAY + timedelta(days=dayOffset, hours=hour), float(forecast), float(actual))


def _runStamp(when):
    """A plan_run tag as planner.py:2358 writes it: UTC, ISO 8601, Z-suffixed.

    Built by converting rather than by formatting the local time with a Z on the end. That
    shortcut stamps a summer run two hours late, which puts every plan out of force and makes
    a correct collect() look broken.
    """
    return when.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --- pure helpers ----------------------------------------------------------------------

def test_byHour_keys_on_local_hour_of_day_across_days():
    rows = [_row(7, 100, 100), _row(7, 100, 100, dayOffset=1), _row(19, 100, 100)]
    hours = fl.byHour(rows)
    assert sorted(hours) == [7, 19]
    assert len(hours[7]) == 2
    assert len(hours[19]) == 1


def test_summarise_ratio_is_energy_weighted_not_a_mean_of_ratios():
    """A big interval that is 10% off and a tiny one that is 100% off. The mean of the two
    ratios would be 1.55; the energy-weighted answer, which is what a profile change would
    have to move, is dominated by the big one."""
    n, fc, act, ratio, _ = fl.summarise([(1000.0, 1100.0), (10.0, 20.0)])
    assert n == 2
    assert fc == pytest.approx(1010.0)
    assert act == pytest.approx(1120.0)
    assert ratio == pytest.approx(1120.0 / 1010.0)
    assert ratio < 1.2


def test_summarise_stderr_ignores_intervals_below_the_ratio_floor():
    """The 1 Wh forecast against 50 Wh measured is a 50x per-interval ratio about nothing.
    It must not reach the +/- column, and must still reach the energy-weighted ratio."""
    pairs = [(500.0, 500.0), (500.0, 500.0), (1.0, 50.0)]
    _, _, _, ratio, se = fl.summarise(pairs)
    assert se == pytest.approx(0.0)          # the two real intervals agree exactly
    assert ratio == pytest.approx(1050.0 / 1001.0)


def test_summarise_stderr_is_none_with_fewer_than_two_usable_intervals():
    assert fl.summarise([(500.0, 500.0)])[4] is None
    assert fl.summarise([(1.0, 50.0), (2.0, 90.0)])[4] is None   # both below the floor


def test_summarise_ratio_is_nan_when_the_forecast_was_zero():
    ratio = fl.summarise([(0.0, 400.0)])[3]
    assert ratio != ratio                     # NaN, and deliberately not 0 or a crash


def test_a_perfect_daily_total_can_hide_opposite_hourly_errors():
    """The failure this whole tool exists to catch. The day balances to within 0.2%, while
    the morning is 60% over and the evening 33% under."""
    rows = [_row(7, 1000, 400), _row(19, 1000, 1600)]
    total = fl.summarise([(f, a) for _, f, a in rows])
    assert total[3] == pytest.approx(1.0)

    hours = fl.byHour(rows)
    assert fl.summarise(hours[7])[3] == pytest.approx(0.4)
    assert fl.summarise(hours[19])[3] == pytest.approx(1.6)


def test_isWeekend_splits_saturday_and_sunday_only():
    # WEDNESDAY + 3 = Saturday, +4 = Sunday, +5 = Monday.
    assert not fl.isWeekend(WEDNESDAY)
    assert fl.isWeekend(WEDNESDAY + timedelta(days=3))
    assert fl.isWeekend(WEDNESDAY + timedelta(days=4))
    assert not fl.isWeekend(WEDNESDAY + timedelta(days=5))


def test_ratioRow_flags_a_thin_bucket_and_leaves_a_full_one_unflagged():
    thin = fl._ratioRow("weekend", [(500.0, 500.0)] * (fl.THIN - 1))
    full = fl._ratioRow("weekday", [(500.0, 500.0)] * fl.THIN)
    assert thin.endswith("thin")
    assert not full.endswith("thin")


def test_days_reads_as_prose_at_the_boundary():
    """A bare %.0f prints "1 days" on exactly the shortest run - the one where the reader is
    least sure what they are looking at."""
    assert fl._days(1.0) == "1 day"
    assert fl._days(1.4) == "1 day"
    assert fl._days(2.75) == "3 days"
    assert fl._days(30.0) == "30 days"


def test_stderr_matches_a_hand_computed_value():
    # values 1, 2, 3: sample variance 1.0, so the standard error is sqrt(1/3).
    assert fl.stderr([1.0, 2.0, 3.0]) == pytest.approx((1.0 / 3.0) ** 0.5)
    assert fl.stderr([5.0]) is None
    assert fl.stderr([]) is None


# --- collect() -------------------------------------------------------------------------

def _fakeInflux(monkeypatch, points, measured):
    monkeypatch.setattr(ix, "planPoints", lambda start, stop, meas: points)
    monkeypatch.setattr(ix, "intervalEnergyWh",
                        lambda field, start, stop, minutes, **kw: measured)


def _planPoint(when, run, load):
    p = {"time": when, "plan_run": run}
    if load is not None:
        p["load_forecast_wh"] = load
    return p


def test_collect_pairs_forecast_with_measured_using_the_plan_in_force(monkeypatch):
    t0 = WEDNESDAY.replace(hour=6)
    t1 = t0 + timedelta(hours=1)
    run = _runStamp(t0)
    points = [_planPoint(t0, run, 300.0), _planPoint(t1, run, 400.0)]
    _fakeInflux(monkeypatch, points, {t0: 350.0, t1: 380.0})

    d = fl.collect(7)
    assert d["planned"] == 2
    assert d["withField"] == 2
    assert [(f, a) for _, f, a in d["rows"]] == [(300.0, 350.0), (400.0, 380.0)]


def test_collect_keeps_zero_forecast_intervals(monkeypatch):
    """The PV tool drops these because a zero forecast at night is a non-event. A zero load
    forecast against real consumption is the opposite: exactly the error worth seeing."""
    t0 = WEDNESDAY.replace(hour=3)
    run = _runStamp(t0)
    _fakeInflux(monkeypatch, [_planPoint(t0, run, 0.0)], {t0: 250.0})

    assert [(f, a) for _, f, a in fl.collect(7)["rows"]] == [(0.0, 250.0)]


def test_collect_counts_missing_field_separately_from_missing_measurement(monkeypatch):
    """withField distinguishes 'the planner is not writing it' from 'the collector has
    nothing yet'. Both produce zero rows and need opposite investigations."""
    t0 = WEDNESDAY.replace(hour=6)
    run = _runStamp(t0)

    _fakeInflux(monkeypatch, [_planPoint(t0, run, None)], {t0: 350.0})
    noField = fl.collect(7)
    assert noField["rows"] == [] and noField["withField"] == 0 and noField["planned"] == 1

    _fakeInflux(monkeypatch, [_planPoint(t0, run, 300.0)], {})
    noActual = fl.collect(7)
    assert noActual["rows"] == [] and noActual["withField"] == 1


def test_collect_returns_none_when_no_plans_were_stored(monkeypatch):
    _fakeInflux(monkeypatch, [], {})
    assert fl.collect(7) is None


def test_collect_ignores_a_plan_run_made_after_the_interval(monkeypatch):
    """rd.inForcePlans' rule, exercised through collect: a run stamped after the interval it
    covers is a forecast made with hindsight and must not be scored."""
    t0 = WEDNESDAY.replace(hour=6)
    late = _runStamp(t0 + timedelta(hours=2))
    _fakeInflux(monkeypatch, [_planPoint(t0, late, 300.0)], {t0: 350.0})

    d = fl.collect(7)
    assert d["rows"] == []
    assert d["planned"] == 0


# --- what main() claims about its own data ----------------------------------------------
#
# The first live run asked for 30 days, got under three, and said "2026-07-03 -> 2026-08-02"
# with a paragraph about weekends filling at 2/7 the rate - directly above a table showing
# more weekend intervals than weekday ones. These cover the reporting, not the arithmetic.

def _synthetic(dayCount, minutes=15, endingSunday=True):
    """A run of complete days ending on a Sunday, so the weekday/weekend counts are known."""
    step = timedelta(minutes=minutes)
    perDay = 1440 // minutes
    last = WEDNESDAY + timedelta(days=4)          # the Sunday of that week
    if not endingSunday:
        last = WEDNESDAY
    rows = []
    for d in range(dayCount):
        day = last - timedelta(days=dayCount - 1 - d)
        rows.extend((day + i * step, 500.0, 500.0) for i in range(perDay))
    return rows


def _runMain(monkeypatch, capsys, rows, asked, minutes=15):
    monkeypatch.setattr(fl, "collect", lambda days: {
        "rows": rows, "runs": 8 * asked, "start": rows[0][0], "stop": rows[-1][0],
        "minutes": minutes, "planned": len(rows), "withField": len(rows)})
    monkeypatch.setattr(ix, "configured", lambda: True)
    assert fl.main([str(asked)]) == 0
    return capsys.readouterr().out


def test_header_reports_the_span_with_data_not_the_span_requested(monkeypatch, capsys):
    out = _runMain(monkeypatch, capsys, _synthetic(3), asked=30)
    first = out.splitlines()[0]
    assert "2026-07-31" in first and "2026-08-02" in first
    assert "2026-07-03" not in out          # the requested start, which has no data behind it
    assert "the plan history does not go back that far" in out
    assert "describes 3 days, not 30 days" in out


def test_no_shortfall_note_when_the_history_covers_the_request(monkeypatch, capsys):
    out = _runMain(monkeypatch, capsys, _synthetic(30), asked=30)
    assert "does not go back that far" not in out


def test_the_two_over_seven_claim_is_withheld_on_a_short_window(monkeypatch, capsys):
    """Three days ending on a Sunday are 2/3 weekend. Stating the long-run rule here would
    contradict the counts printed immediately above it."""
    out = _runMain(monkeypatch, capsys, _synthetic(3), asked=30)
    assert "Weekday/weekend split:" in out
    assert "2/7" not in out
    assert "says nothing about the long run" in out


def test_the_two_over_seven_claim_is_made_once_the_window_can_support_it(monkeypatch, capsys):
    out = _runMain(monkeypatch, capsys, _synthetic(30), asked=30)
    assert "Weekends are 2/7 of the calendar" in out


def test_the_wait_projection_uses_the_observed_rate_not_the_theoretical_one(monkeypatch, capsys):
    """Two days of 15-minute data give each hour 8 intervals, so THIN=20 is 3 days off at the
    observed 4/day. The projection must say so, and must flag itself as a floor."""
    out = _runMain(monkeypatch, capsys, _synthetic(2), asked=30)
    assert "TOO THIN to conclude anywhere" in out
    assert "4.0 intervals per hour-of-day per day" in out
    assert "in about 3 more days" in out
    assert "treat it as a floor" in out


def test_no_wait_is_projected_from_a_single_day(monkeypatch, capsys):
    """One day is not a rate. Projecting from it produces a confident number built on one
    observation, which is worse than declining to answer."""
    out = _runMain(monkeypatch, capsys, _synthetic(1), asked=30)
    assert "Too short a span to project" in out
    assert "more day" not in out.split("Too short a span")[1]


# --- CLI -------------------------------------------------------------------------------

def _run(args):
    env = dict(os.environ)
    for key in ("INFLUX_URL", "INFLUX_HOST", "INFLUX_TOKEN", "INFLUX_TOKEN_PLANNING"):
        env.pop(key, None)
    env["INFLUX_ENV_FILE"] = "/nonexistent/.env"
    return subprocess.run([PYTHON, SCRIPT] + args, capture_output=True, text=True,
                          timeout=30, env=env)


def test_help_flag_exits_zero():
    result = _run(["-h"])
    assert result.returncode == 0
    assert "Measure the house load forecast" in result.stdout


def test_non_numeric_days_fails_cleanly():
    result = _run(["last-week"])
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "whole number of days" in result.stderr


def test_zero_days_is_rejected():
    result = _run(["0"])
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "at least 1 day" in result.stderr


def test_valid_days_reaches_the_influxdb_check():
    result = _run(["14"])
    assert result.returncode == 2
    assert "InfluxDB is not configured" in result.stdout


def test_default_days_when_omitted():
    result = _run([])
    assert result.returncode == 2
    assert "InfluxDB is not configured" in result.stdout
