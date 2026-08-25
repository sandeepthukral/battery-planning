"""Sun position and elevation-loss-curve interpolation, in one place.

CODE-REVIEW.md D5. Before this, the same NOAA solar-elevation approximation existed
three times - planner.py's solarElevation() (date+hour strings, HTTP-resolved
site location, its own +30-minute hour-midpoint), fit_pv_elevation.py's solarElevation()
(lat/lon + an exact instant, because the planner's version cannot resolve anything finer
than an hour - see that file's docstring), and clean_backtest_csv.py's solar_elevation()
(a local datetime, module-level LAT/LON globals). All three compute the identical
formula; fit_pv_elevation.py's checkAgreement() existed specifically to catch the copies
drifting apart, at 1-degree tolerance, because nothing forced them to agree exactly.

elevation() below is that formula, written once, taking the most general inputs (lat,
lon, an exact aware instant) so every caller adapts to it rather than the other way
round. fit_pv_elevation.py no longer needs checkAgreement() as a drift guard - there is
only one implementation left to drift from itself - though it still keeps its own
integration test that planner.py's *wrapper* around this produces the same
answer at whole hours, which is a different and still-useful thing to check.

interpolate() is the (elevation_deg, retained_fraction) curve lookup - the same linear
interpolation planner.py's pvElevationCalibration() and fit_pv_elevation.py's
_interp() each implemented separately.
"""
import math


def elevation(lat, lon, when):
    """Sun elevation in degrees, NOAA approximation, at an exact instant.

    lat/lon in degrees (lon east-positive). `when` is an aware datetime in any
    timezone - only its UTC instant matters.
    """
    n = when.timestamp() / 86400.0 + 2440587.5 - 2451545.0
    meanLong = math.radians((280.460 + 0.9856474 * n) % 360)
    meanAnom = math.radians((357.528 + 0.9856003 * n) % 360)
    eclipLong = (meanLong + math.radians(1.915) * math.sin(meanAnom)
                 + math.radians(0.020) * math.sin(2 * meanAnom))
    obliquity = math.radians(23.439 - 0.0000004 * n)
    declination = math.asin(math.sin(obliquity) * math.sin(eclipLong))
    rightAsc = math.atan2(math.cos(obliquity) * math.sin(eclipLong), math.cos(eclipLong))
    gmst = (18.697374558 + 24.06570982441908 * n) % 24
    hourAngle = math.radians((gmst * 15.0 + lon) % 360) - rightAsc
    latRad = math.radians(lat)
    return math.degrees(math.asin(
        math.sin(latRad) * math.sin(declination)
        + math.cos(latRad) * math.cos(declination) * math.cos(hourAngle)))


def interpolate(curve, x):
    """Linear interpolation over a sorted [(x0, y0), (x1, y1), ...] curve.

    Flat below the first breakpoint and above the last, same convention both
    original copies used (pvElevationLossCurve is normalised to a high-sun plateau,
    so "flat above the last point" is the intended behaviour, not a missing case).
    """
    if x <= curve[0][0]:
        return curve[0][1]
    for i in range(1, len(curve)):
        x0, y0 = curve[i - 1]
        x1, y1 = curve[i]
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0) if x1 > x0 else y1
    return curve[-1][1]
