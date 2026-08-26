"""Which plan the day report scores, and whether that plan is physically possible.

The bug these cover shipped working, looked right, and was wrong by 12 kWh. report_day.py
scored each interval against the run in force for it; every run restarts from the measured
SoC; so the chain summed one opening move from each of the day's 24 runs, every one of them
believing the battery was still full. On 2026-08-21 it published "the plan said it could save 7.54 EUR" for a day
whose committed plan offered 2.41 and whose battery earned 5.82.

Rows are hand-built, same style as test_report_day_forecast.py: nothing here needs a
database, and the two functions under test are pure.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import influx_source as ix
import report_day as rd

TZ = ix.LOCAL_TZ or timezone.utc
START = datetime(2026, 8, 21, 0, 0, tzinfo=TZ)
STOP = START + timedelta(days=1)
MINUTES = 15
INTERVALS = 96


def _points(run, count, first=0):
    """A stored run covering `count` intervals from interval `first` of the day."""
    return [{"plan_run": run, "time": START + timedelta(minutes=MINUTES * (first + i))}
            for i in range(count)]


# --- which run is the committed one -------------------------------------------------------

def test_the_latest_run_covering_the_first_interval_wins():
    points = (_points("2026-08-20T20:05:00Z", INTERVALS)
              + _points("2026-08-20T21:05:00Z", INTERVALS))
    assert rd.committedRun(points, START, STOP, MINUTES) == ("2026-08-20T21:05:00Z", INTERVALS)


def test_a_run_stamped_after_midnight_still_counts_if_it_covers_midnight():
    """The :55/:05 skew, which is the reason selection is by coverage and not by tag. The
    planner fires at 23:55 and its point lands stamped 00:05; a tag-based cut at the day
    boundary would throw that run away and score the day on the 22:55 one instead - an hour
    staler, in the direction nobody would ever notice."""
    late = "2026-08-20T22:05:00Z"          # 00:05 local on the 21st, covering from 00:00
    points = _points("2026-08-20T21:05:00Z", INTERVALS) + _points(late, INTERVALS)
    assert rd.committedRun(points, START, STOP, MINUTES)[0] == late


def test_a_run_made_during_the_day_is_never_chosen():
    """It has no opinion about the morning, and scoring the day on it would be hindsight -
    the same rule inForcePlans() applies per interval, applied to the window."""
    points = (_points("2026-08-20T21:05:00Z", INTERVALS)
              + _points("2026-08-21T10:05:00Z", INTERVALS - 40, first=40))
    assert rd.committedRun(points, START, STOP, MINUTES)[0] == "2026-08-20T21:05:00Z"


def test_full_coverage_beats_a_later_run_that_stops_early():
    """A truncated horizon is not a plan for the day, however recent it is."""
    points = (_points("2026-08-20T20:05:00Z", INTERVALS)
              + _points("2026-08-20T21:05:00Z", 40))
    assert rd.committedRun(points, START, STOP, MINUTES) == ("2026-08-20T20:05:00Z", INTERVALS)


def test_falls_back_to_the_best_covered_run_when_none_spans_the_day():
    """A day the planner started late still deserves a report; the header says how much of
    it is judged."""
    points = _points("2026-08-20T21:05:00Z", 40) + _points("2026-08-20T20:05:00Z", 60)
    assert rd.committedRun(points, START, STOP, MINUTES) == ("2026-08-20T20:05:00Z", 60)


def test_no_run_predates_the_window():
    points = _points("2026-08-21T10:05:00Z", 20, first=40)
    assert rd.committedRun(points, START, STOP, MINUTES) == (None, 0)


def test_an_unparseable_run_tag_is_skipped_rather_than_crashing():
    points = _points("not-a-timestamp", INTERVALS) + _points("2026-08-20T21:05:00Z", INTERVALS)
    assert rd.committedRun(points, START, STOP, MINUTES)[0] == "2026-08-20T21:05:00Z"


# --- does the scored case conserve energy -------------------------------------------------

def _row(i, planExport=0.0, planImport=0.0, planPv=0.0, planLoad=0.0, planSoc=0.0,
         actExport=0.0, actImport=0.0, actPv=0.0, actLoad=0.0, actSoc=0.0, priceSell=0.4):
    return {"time": START + timedelta(minutes=MINUTES * i),
            "priceBuy": 0.43, "priceSell": priceSell,
            "planImport": planImport, "planExport": planExport, "planPv": planPv,
            "planLoad": planLoad, "planSoc": planSoc,
            "planCharge": 0.0, "planDischarge": 0.0,
            "actImport": actImport, "actExport": actExport, "actPv": actPv,
            "actLoad": actLoad, "actSoc": actSoc,
            "actCharge": 0.0, "actDischarge": 0.0}


def test_a_plan_that_conserves_energy_passes():
    """Opens with 10 kWh stored, closes empty, and the 10 kWh leaves as export less a
    plausible round-trip loss."""
    rows = [_row(0, planSoc=10000.0), _row(1, planSoc=0.0, planExport=9500.0)]
    b = rd.energyBalance(rows, "plan")
    assert b["ok"] and b["residual"] == 500.0


def test_the_band_is_tighter_against_energy_from_nowhere_than_against_loss():
    """Same magnitude, opposite sign, opposite verdict - and deliberately so. A day can lose
    energy to the inverter; it cannot invent it."""
    loss = [_row(0, planSoc=10000.0), _row(1, planSoc=0.0, planExport=9000.0)]
    gain = [_row(0, planSoc=10000.0), _row(1, planSoc=0.0, planExport=11000.0)]
    assert rd.energyBalance(loss, "plan")["ok"]
    assert not rd.energyBalance(gain, "plan")["ok"]


def test_a_plan_exporting_energy_it_never_had_fails():
    """The 2026-08-21 shape, shrunk: the chain exported 25.02 kWh out of a 29.41 kWh supply
    against a 16.41 kWh load. Nothing in the window produced the difference."""
    rows = [_row(0, planSoc=10000.0), _row(1, planSoc=0.0, planExport=22000.0)]
    b = rd.energyBalance(rows, "plan")
    assert not b["ok"]
    assert b["residual"] < 0                      # energy from nowhere, not merely a big day


def test_the_balance_ignores_intervals_missing_a_term():
    """load_forecast_wh is optional in the plan measurement. A day without it is unchecked,
    not failed."""
    rows = [_row(0, planSoc=10000.0), _row(1, planSoc=0.0)]
    rows[1]["planLoad"] = None
    assert rd.energyBalance(rows, "plan") is None


def test_one_interval_is_not_a_balance():
    assert rd.energyBalance([_row(0, planSoc=10000.0)], "plan") is None


def test_the_actual_side_is_checked_too():
    rows = [_row(0, actSoc=10000.0), _row(1, actSoc=0.0, actExport=9500.0)]
    assert rd.energyBalance(rows, "actual")["ok"]


def test_section_checks_refuses_on_the_plan_side_only():
    """A partial day's actuals must not block a report; an impossible plan must."""
    sound = [_row(0, planSoc=10000.0), _row(1, planSoc=0.0, planExport=9500.0)]
    broken = [_row(0, planSoc=10000.0), _row(1, planSoc=0.0, planExport=22000.0)]
    assert rd.sectionChecks({"rows": sound}) is True
    assert rd.sectionChecks({"rows": broken}) is False


# --- what section 1 says ------------------------------------------------------------------

def _money(rows, rollingRows=None, run="2026-08-20T21:05:00Z"):
    return {"rows": rows, "rollingRows": rollingRows, "committedRun": run}


def test_the_saving_line_comes_from_the_committed_run_not_the_chain(capsys):
    """The regression in one assertion: the chain exports twice what the committed run does,
    and the headline must ignore it."""
    rows = [_row(0, planSoc=5000.0, planExport=1000.0, actSoc=5000.0, actLoad=500.0, actPv=0.0),
            _row(1, planSoc=0.0, planExport=4000.0, actSoc=0.0, actLoad=500.0, actPv=0.0)]
    chain = [_row(0, planSoc=5000.0, planExport=1000.0),
             _row(1, planSoc=0.0, planExport=9000.0)]
    rd.sectionMoney(_money(rows, chain))
    out = capsys.readouterr().out

    said = next(l for l in out.splitlines() if "the plan said it could save" in l)
    bound = next(l for l in out.splitlines() if "best-of-replans bound" in l)
    assert float(said.split()[-2]) < float(bound.split()[-2])
    assert "Not achievable" in out


def test_the_bound_is_labelled_a_diagnostic_and_never_a_saving(capsys):
    rows = [_row(0, planSoc=5000.0, actSoc=5000.0, actLoad=500.0),
            _row(1, planSoc=0.0, planExport=4000.0, actSoc=0.0, actLoad=500.0)]
    rd.sectionMoney(_money(rows, list(rows)))
    out = capsys.readouterr().out
    assert "best-of-replans bound (diagnostic)" in out
    assert out.count("the plan said it could save") == 1


def test_no_chain_no_bound_line(capsys):
    """report_window.py shares collectWindow() and does not build the chain."""
    rows = [_row(0, planSoc=5000.0, actSoc=5000.0, actLoad=500.0),
            _row(1, planSoc=0.0, actSoc=0.0, actLoad=500.0)]
    rd.sectionMoney(_money(rows, None))
    assert "best-of-replans" not in capsys.readouterr().out


def test_a_plan_closing_richer_than_reality_is_credited_for_it(capsys):
    """Two cases that end at different SoC have not been compared, they have been described.
    The plan here keeps 2 kWh reality spent, which is 0.80 EUR it is holding, not losing."""
    rows = [_row(0, planSoc=5000.0, actSoc=5000.0, actLoad=500.0),
            _row(1, planSoc=2000.0, actSoc=0.0, actLoad=500.0, priceSell=0.4)]
    rd.sectionMoney(_money(rows))
    out = capsys.readouterr().out
    credit = next(l for l in out.splitlines() if "closes" in l and "richer" in l)
    assert "2.00 kWh richer" in credit and "+0.80 EUR" in credit
    assert "like for like" in out


def test_a_plan_closing_emptier_than_reality_is_charged_for_it(capsys):
    """The mirror case, and the one that would flatter a plan if it were left out."""
    rows = [_row(0, planSoc=5000.0, actSoc=5000.0, actLoad=500.0),
            _row(1, planSoc=0.0, actSoc=2000.0, actLoad=500.0, priceSell=0.4)]
    rd.sectionMoney(_money(rows))
    out = capsys.readouterr().out
    credit = next(l for l in out.splitlines() if "closes" in l)
    assert "2.00 kWh emptier" in credit and "-0.80 EUR" in credit


def test_no_credit_line_when_both_cases_close_together(capsys):
    rows = [_row(0, planSoc=5000.0, actSoc=5000.0, actLoad=500.0),
            _row(1, planSoc=20.0, actSoc=0.0, actLoad=500.0)]
    rd.sectionMoney(_money(rows))
    assert "richer" not in capsys.readouterr().out
