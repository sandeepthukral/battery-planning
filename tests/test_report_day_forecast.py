"""report_day.py's section 3, called directly with hand-built rows and its output captured.

Separate from test_report_day_cli.py on purpose: that file is scoped to argument parsing,
and says so in its docstring - every case there is decided before the database is reached.
sectionForecast() takes plain row dicts and only prints, so it needs no database either, but
it is a different kind of test and mixing the two would make that file's claim untrue.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import influx_source as ix
import report_day as rd

TZ = ix.LOCAL_TZ or timezone.utc
MIDNIGHT = datetime(2026, 7, 29, 0, 0, tzinfo=TZ)


def _row(hour, planLoad, actLoad, planPv=None, actPv=None):
    return {"time": MIDNIGHT + timedelta(hours=hour),
            "planLoad": planLoad, "actLoad": actLoad,
            "planPv": planPv, "actPv": actPv, "planPvRaw": None}


def _loadTable(out):
    """The load-by-hour rows only, as (hour, forecast, measured, error) tuples."""
    lines = out.splitlines()
    start = next(i for i, l in enumerate(lines) if "load by hour" in l) + 1
    rows = []
    for line in lines[start:]:
        parts = line.split()
        if not parts or not parts[0].endswith(":00"):
            break
        rows.append((parts[0], float(parts[1]), float(parts[2]), " ".join(parts[3:])))
    return rows


def test_load_by_hour_table_appears_and_carries_every_hour(capsys):
    rd.sectionForecast([_row(7, 900, 900), _row(19, 1200, 1200)])
    rows = _loadTable(capsys.readouterr().out)
    assert [r[0] for r in rows] == ["07:00", "19:00"]


def test_opposite_hourly_errors_that_cancel_in_the_total_are_still_visible(capsys):
    """The reason the table exists. The day totals 2200 Wh forecast against 2200 measured -
    a flawless report by the old single line - while the morning forecast is 150% over and
    the evening one 33% under. pctErr is (forecast - actual) / actual, so the sign describes
    the forecast, not the house.

    The evening row is the one that costs money: an under-forecast there is an undersized
    overnight reserve, which the 25% flat margin does not know to widen."""
    rd.sectionForecast([_row(7, 1000, 400), _row(19, 1200, 1800)])
    out = capsys.readouterr().out

    total = next(l for l in out.splitlines() if l.strip().startswith("load   forecast"))
    assert "2.20 kWh" in total and "+0%" in total

    rows = dict((r[0], r[3]) for r in _loadTable(out))
    assert rows["07:00"] == "+150% over"
    assert rows["19:00"] == "-33% under"


def test_load_table_is_printed_even_when_there_is_no_pv_at_all(capsys):
    """PV and load fail independently, which is why section 3 keeps them apart. A night with
    no PV rows must not take the load table down with it."""
    rd.sectionForecast([_row(2, 300, 350), _row(3, 300, 280)])
    out = capsys.readouterr().out
    assert "load by hour" in out
    assert "PV by hour" not in out


def test_no_load_rows_prints_no_load_table(capsys):
    rd.sectionForecast([_row(12, None, None, planPv=500, actPv=450)])
    out = capsys.readouterr().out
    assert "load by hour" not in out
    assert "PV by hour" in out


def test_hours_with_neither_forecast_nor_measurement_are_skipped(capsys):
    rd.sectionForecast([_row(3, 0, 0), _row(19, 1200, 1200)])
    assert [r[0] for r in _loadTable(capsys.readouterr().out)] == ["19:00"]


def test_a_zero_forecast_hour_with_real_load_is_not_skipped(capsys):
    """The complement of the case above, and the one that matters: an hour the profile
    expected nothing in is exactly where a silent skip would hide the worst error."""
    rd.sectionForecast([_row(3, 0, 400)])
    table = _loadTable(capsys.readouterr().out)
    assert [r[0] for r in table] == ["03:00"]
    assert table[0][1] == 0.0 and table[0][2] == 400.0
