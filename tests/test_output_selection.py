"""_rowsToOutput() (CODE-REVIEW.md D8): which rows a multi-day backtest run writes to
its output file. Only the "everything" branch is exercised by the golden-file tests
(plan-now.sh always sets BT_END=tomorrow, which takes that branch) - the other two
only fire inside a multi-day run-matrix.sh backtest, so they need their own coverage.
"""
from datetime import datetime, timedelta


def _row(day, hour):
    return [0, 0.20, "%s %02d:00" % (day, hour), "%s %02d:00" % (day, hour),
            0, 0, 0, 0.20, 0.20]


def _priceListAndSchedule(entries):
    """entries: [(day, hour), ...]. Builds a matching priceList and a trivial schedule
    (one dict per row, index carried in "costs" so tests can identify which rows came
    back without depending on dict identity)."""
    priceList = [_row(day, hour) for day, hour in entries]
    schedule = [{"costs": i} for i in range(len(entries))]
    return priceList, schedule


def test_last_day_returns_everything(planner):
    """runDate + 1 day == endDateObject: the live path always takes this branch
    (plan-now.sh sets BT_END=tomorrow), and the last day of a multi-day backtest does
    too - no date filtering at all, regardless of what the rows actually contain."""
    priceList, schedule = _priceListAndSchedule([
        ("2026-01-01", 10), ("2026-01-01", 14), ("2026-01-02", 8),
    ])
    runDate = datetime.strptime("20260101", "%Y%m%d")
    startDateObject = runDate
    endDateObject = runDate + timedelta(days=1)
    rows = planner._rowsToOutput(runDate, startDateObject, endDateObject, priceList, schedule)
    assert [nr for nr, _ in rows] == [0, 1, 2]


def test_first_day_of_a_multi_day_run_stops_before_15_00_the_next_day(planner):
    """runDate == startDateObject but NOT the last day: everything on runDate itself,
    plus the next day only up to (excluding) 15:00 - the boundary where the FOLLOWING
    day's own run takes over."""
    priceList, schedule = _priceListAndSchedule([
        ("2026-01-01", 0), ("2026-01-01", 23),      # all of day 1: included
        ("2026-01-02", 0), ("2026-01-02", 14),      # day 2 before 15:00: included
        ("2026-01-02", 15), ("2026-01-02", 20),     # day 2 from 15:00: NOT this run's rows
    ])
    runDate = datetime.strptime("20260101", "%Y%m%d")
    startDateObject = runDate
    endDateObject = datetime.strptime("20260103", "%Y%m%d")   # 2 days later: not the last day
    rows = planner._rowsToOutput(runDate, startDateObject, endDateObject, priceList, schedule)
    assert [nr for nr, _ in rows] == [0, 1, 2, 3]


def test_middle_day_starts_at_15_00_and_stops_before_15_00_the_next_day(planner):
    """A day that is neither the first nor the last of the run: picks up exactly where
    the previous iteration's file left off (15:00 runDate) and hands off at the same
    boundary the next day, so nothing is written twice and nothing is skipped."""
    priceList, schedule = _priceListAndSchedule([
        ("2026-01-05", 10), ("2026-01-05", 14),     # before 15:00 runDate: already written by the PREVIOUS run
        ("2026-01-05", 15), ("2026-01-05", 23),     # from 15:00 runDate: this run's rows
        ("2026-01-06", 0), ("2026-01-06", 14),      # next day before 15:00: still this run's rows
        ("2026-01-06", 15),                          # next day from 15:00: the FOLLOWING run's rows
    ])
    runDate = datetime.strptime("20260105", "%Y%m%d")
    startDateObject = datetime.strptime("20260101", "%Y%m%d")   # some earlier day
    endDateObject = datetime.strptime("20260110", "%Y%m%d")     # well beyond runDate+1
    rows = planner._rowsToOutput(runDate, startDateObject, endDateObject, priceList, schedule)
    assert [nr for nr, _ in rows] == [2, 3, 4, 5]


def test_empty_schedule_returns_no_rows(planner):
    runDate = datetime.strptime("20260101", "%Y%m%d")
    rows = planner._rowsToOutput(runDate, runDate, runDate + timedelta(days=1), [], [])
    assert rows == []
