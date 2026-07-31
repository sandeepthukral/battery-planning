"""Small priceList-manipulating helpers that still read/write module globals directly
(dropHistoryFromPricelist, getSOC) rather than taking parameters - narrower in scope
than the A2 functions, so CODE-REVIEW.md's Stage 1 fixes them in place rather than
parametrizing them. Each test sets the globals the function under test needs on the
freshly-loaded module before calling it.
"""
from datetime import datetime, timedelta

import pytest


def _row(seq, hour, day="2026-01-01"):
    return [seq, 0.20, "%s %02d:00" % (day, hour), "%s %02d:00" % (day, hour), 0, 0, 0, 0.20, 0.20]


# --- dropHistoryFromPricelist(): C2 -------------------------------------------------


def test_drops_the_requested_number_of_intervals(planner):
    planner.hourAvgPlanning = True
    planner.runDate = datetime.strptime("20260101", "%Y%m%d")
    planner.priceList = [_row(i, i) for i in range(24)]
    planner.dropHistoryFromPricelist(5)
    assert len(planner.priceList) == 19
    assert planner.priceList[0][0] == 5   # hour 5 is now first


def test_quarter_hour_mode_drops_four_per_hour(planner):
    planner.hourAvgPlanning = False
    planner.runDate = datetime.strptime("20260101", "%Y%m%d")   # >= 2025-10-01
    planner.priceList = [_row(i, i // 4) for i in range(24)]     # 24 quarter-hours = 6 hours
    planner.dropHistoryFromPricelist(2)
    assert len(planner.priceList) == 16   # 2 hours x 4 = 8 dropped


def test_clamps_instead_of_raising_when_fewer_intervals_than_requested(planner, capsys):
    """CODE-REVIEW.md C2: this used to raise IndexError. A partial price fetch (fewer
    intervals than the run hour implies) must warn and clamp, not crash."""
    planner.hourAvgPlanning = True
    planner.runDate = datetime.strptime("20260101", "%Y%m%d")
    planner.priceList = [_row(i, i) for i in range(3)]   # only 3 intervals available
    planner.dropHistoryFromPricelist(10)                 # asks to drop 10
    assert planner.priceList == []                       # clamped to what existed
    assert "WARNING" in capsys.readouterr().out


def test_empty_price_list_does_not_raise(planner, capsys):
    planner.hourAvgPlanning = True
    planner.runDate = datetime.strptime("20260101", "%Y%m%d")
    planner.priceList = []
    planner.dropHistoryFromPricelist(5)
    assert planner.priceList == []


# --- getSOC(): C5 --------------------------------------------------------------------


def _schedule(n):
    return [{"soc": 1000 * i} for i in range(n)]


def test_finds_the_matching_hour(planner):
    planner.priceList = [_row(i, i) for i in range(24)]
    assert planner.getSOC(15, _schedule(24)) == 15000


def test_finds_the_last_match_when_an_hour_repeats(planner):
    """Quarter-hour planning: hour 15 appears up to four times (15:00, 15:15, ...).
    Searching backwards from the end must return the LAST (latest) matching interval,
    i.e. the SoC at the end of that hour, not the first quarter of it."""
    planner.priceList = [_row(0, 15), _row(1, 15), _row(2, 15), _row(3, 16)]
    assert planner.getSOC(15, _schedule(4)) == 2000   # index 2, the last "hour 15" row


def test_raises_when_the_hour_is_not_in_the_window(planner):
    """CODE-REVIEW.md C5: this used to silently wrap to priceList[-1] via Python's
    negative-index semantics and return the wrong SoC instead of failing - carrying
    that error into every later day of a multi-day backtest with nothing reporting it."""
    planner.priceList = [_row(i, i) for i in range(10)]   # hours 0-9 only
    with pytest.raises(ValueError, match="hour 15"):
        planner.getSOC(15, _schedule(10))


def test_raises_on_empty_price_list(planner):
    planner.priceList = []
    with pytest.raises(ValueError):
        planner.getSOC(15, [])


# --- getUserInput()'s midnight-race guard: C7 -----------------------------------------


def test_live_run_refuses_when_BT_START_disagrees_with_today(planner, monkeypatch):
    """CODE-REVIEW.md C7: plan-now.sh computes BT_START from the shell's `date` before
    this process starts; this file computes its own `today` separately at import time.
    A run straddling midnight between those two moments must refuse rather than
    silently plan the wrong day down the historical branch. BT_INITCHARGE=influx is
    what marks this as a live run - the guard must not fire for an ordinary backtest,
    which legitimately plans a date far from today."""
    yesterday = planner.today - timedelta(days=1)
    monkeypatch.setenv("BT_START", yesterday.strftime("%Y%m%d"))
    monkeypatch.setenv("BT_INITCHARGE", "influx")
    with pytest.raises(SystemExit) as excinfo:
        planner.getUserInput()
    assert excinfo.value.code == 6


def test_backtest_run_is_not_affected_by_the_guard(planner, monkeypatch):
    """BT_INITCHARGE left at a plain number (the backtest case): BT_START may
    legitimately be nowhere near today, and getUserInput() must not raise for it."""
    monkeypatch.setenv("BT_START", "20250701")
    monkeypatch.setenv("BT_END", "20250702")
    monkeypatch.setenv("BT_INITCHARGE", "5000")
    planner.getUserInput()   # must not raise
    assert planner.startdate == "20250701"
