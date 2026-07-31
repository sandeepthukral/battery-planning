"""solar.py: the one shared implementation of solar elevation and curve interpolation
(CODE-REVIEW.md D5). Before this, the same formula existed separately in
Marstek-planning.py, fit_pv_elevation.py and clean_backtest_csv.py.
"""
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import solar

LAT, LON = 52.5, 5.5   # Flevoland - same site every caller uses
AMS = ZoneInfo("Europe/Amsterdam")


def test_elevation_near_zero_at_sunrise_june_solstice():
    # 2026-06-21 sunrise in Amsterdam is close to 05:15 local
    when = datetime(2026, 6, 21, 5, 15, tzinfo=AMS)
    elev = solar.elevation(LAT, LON, when)
    assert -3.0 <= elev <= 3.0


def test_elevation_near_maximum_at_june_solstice_noon():
    # Solar noon at 52.5N on the June solstice: max elevation ~= 90 - 52.5 + 23.44 = 61
    when = datetime(2026, 6, 21, 13, 15, tzinfo=AMS)   # ~13:15 local solar noon (CEST)
    elev = solar.elevation(LAT, LON, when)
    assert 58.0 <= elev <= 63.0


def test_elevation_low_at_december_solstice_noon():
    # Max elevation at the December solstice: 90 - 52.5 - 23.44 = ~14 degrees
    when = datetime(2026, 12, 21, 12, 30, tzinfo=AMS)   # CET, no DST
    elev = solar.elevation(LAT, LON, when)
    assert 11.0 <= elev <= 17.0


def test_elevation_negative_at_midnight():
    when = datetime(2026, 3, 15, 0, 0, tzinfo=AMS)
    assert solar.elevation(LAT, LON, when) < 0


def test_elevation_moderate_at_equinox_noon():
    # Equinox noon elevation at 52.5N: 90 - 52.5 = ~37.5 degrees
    when = datetime(2026, 3, 20, 12, 30, tzinfo=AMS)   # CET
    elev = solar.elevation(LAT, LON, when)
    assert 34.0 <= elev <= 41.0


def test_elevation_is_timezone_independent():
    """The same instant, expressed in two different timezones, must give the same
    elevation - only the UTC instant matters, per solar.elevation()'s own contract."""
    local = datetime(2026, 6, 21, 13, 15, tzinfo=AMS)
    utc = local.astimezone(ZoneInfo("UTC"))
    assert solar.elevation(LAT, LON, local) == solar.elevation(LAT, LON, utc)


# --- interpolate() --------------------------------------------------------------------

CURVE = [(0, 0.20), (10, 0.50), (20, 0.70), (40, 1.00)]


def test_interpolate_flat_below_first_breakpoint():
    assert solar.interpolate(CURVE, -5) == 0.20
    assert solar.interpolate(CURVE, 0) == 0.20


def test_interpolate_flat_above_last_breakpoint():
    assert solar.interpolate(CURVE, 40) == 1.00
    assert solar.interpolate(CURVE, 90) == 1.00


def test_interpolate_exactly_on_a_breakpoint():
    assert solar.interpolate(CURVE, 10) == 0.50
    assert solar.interpolate(CURVE, 20) == 0.70


def test_interpolate_midway_between_two_breakpoints():
    # midway between (0, 0.20) and (10, 0.50): 0.35
    assert solar.interpolate(CURVE, 5) == 0.35
    # midway between (20, 0.70) and (40, 1.00): 0.85
    assert solar.interpolate(CURVE, 30) == 0.85
