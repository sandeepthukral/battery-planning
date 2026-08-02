"""Hourly weather for the site, from Open-Meteo, in one place.

Two endpoints, one model family, one grid point:

    fetchArchive(start, stop)               past hours, the archive API
    fetchForecast(pastDays, forecastDays)   past and future hours, the forecast API

Both return the same shape, so a caller that writes points does not care which it called.

WHY OPEN-METEO AND NOT KNMI. KNMI's klimatologie/uurgegevens is free, needs no key, and is
the official Dutch observation record - but it lags. Checked on 2026-08-02: the most recent
hour available was 31 July. That rules it out for anything ongoing, and for any question
about today. KNMI's Open Data API does carry near-real-time observations and HARMONIE
forecasts, but in netcdf/HDF5 and behind KNMI_API_KEY.

The deeper reason is consistency rather than convenience. Any use of this data ends in
fitting a coefficient on past weather and then applying it to forecast weather. Fit on KNMI
station observations and apply to a different model's grid forecast, and the difference
between the two sources is baked into the coefficient, where no amount of accumulated data
will ever reveal it. Past and future must come from the same model at the same point. KNMI
stays valuable as an independent cross-check - station 269, Lelystad Airport - which is a
different job from being the pipeline.

TIMEZONE. Requests are made in UTC and converted to local afterwards, rather than asking
Open-Meteo for Europe/Amsterdam directly. The API would answer with naive local strings, and
on the October transition "02:00" occurs twice with no way to tell the two apart. Both would
resolve to the same instant, and the second would silently overwrite the first in InfluxDB.
UTC has no such hour.

UNITS. Open-Meteo defaults wind to km/h; this asks for m/s explicitly. Every field is
converted to the unit its name states before it leaves this module.
"""
from datetime import datetime, timedelta, timezone

import requests

import http_config
import influx_source as ix

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Almere centre. The planner's own siteLatitude/siteLongitude (52.5/5.5, Marstek-planning.py)
# is used for sun elevation, where 15 km is a fraction of a degree and does not matter. It
# does matter here: cloud cover is a local thing, and 52.5/5.5 is open water north-east of
# the city. Overridable so this file does not have to be edited per site.
DEFAULT_LAT = 52.3708
DEFAULT_LON = 5.2158

# Open-Meteo's name for the variable -> the field name written to InfluxDB, and the factor
# to get there. The field name carries its unit because the value alone cannot: 21.0 could be
# degrees or knots, and a mislabelled column is the kind of error that reads as weather.
VARIABLES = [
    ("temperature_2m", "temperature_c", 1.0),
    ("cloud_cover", "cloud_cover_pct", 1.0),
    ("shortwave_radiation", "radiation_w_m2", 1.0),
    ("wind_speed_10m", "wind_ms", 1.0),
    ("relative_humidity_2m", "humidity_pct", 1.0),
]

MEASUREMENT_OBSERVED = "weather_observed"
MEASUREMENT_FORECAST = "weather_forecast"
SOURCE_TAG = "open-meteo"


def config():
    """Site coordinates, resolved the way every other setting in this project is."""
    def number(key, default):
        raw = ix.envValue(key)
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            raise RuntimeError("%s must be a number, got %r" % (key, raw))

    return {"lat": number("WEATHER_LAT", DEFAULT_LAT),
            "lon": number("WEATHER_LON", DEFAULT_LON)}


def _get(url, params):
    c = config()
    params = dict(params)
    params.update({"latitude": c["lat"], "longitude": c["lon"],
                   "hourly": ",".join(v[0] for v in VARIABLES),
                   "wind_speed_unit": "ms",
                   "timezone": "UTC"})
    resp = requests.get(url, params=params, timeout=http_config.HTTP_TIMEOUT)
    if resp.status_code >= 400:
        # Open-Meteo puts the useful part in the body - which parameter it rejected - and
        # raise_for_status() throws all of it away. Same reasoning as influx_source.writePoints.
        raise RuntimeError("weather request failed: HTTP %d %s"
                           % (resp.status_code, resp.text[:400]))
    return resp.json()


def _parseHourly(payload):
    """(rows, nullHours, point) from an Open-Meteo response.

    rows is [(awareDatetime, {field: value}), ...] in local time, ordered.

    An hour with no values at all is dropped rather than zero-filled, and counted, on the
    same principle as intervalEnergyWh()'s coverage gate: a missing hour has to look
    missing. The forecast API's past_days window reaches further back than it holds data
    for, so a run asking for 92 past days gets a few weeks of nulls at the front - that is
    normal, and it is why the count is returned rather than warned about here.

    An hour missing only SOME variables keeps the ones it has. linePoint() drops None
    fields, so the point is written narrower rather than not at all.
    """
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    rows = []
    nullHours = 0
    for i, stamp in enumerate(times):
        fields = {}
        for name, field, factor in VARIABLES:
            series = hourly.get(name)
            value = series[i] if series is not None and i < len(series) else None
            if value is not None:
                fields[field] = float(value) * factor
        if not fields:
            nullHours += 1
            continue
        when = datetime.strptime(stamp, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
        rows.append((when.astimezone(ix.LOCAL_TZ or timezone.utc), fields))
    point = (payload.get("latitude"), payload.get("longitude"))
    return rows, nullHours, point


def fetchArchive(start, stop):
    """Observed hours covering two LOCAL dates inclusive. start/stop are date or datetime.

    A day either side is requested and then trimmed off. The request is made in UTC (see the
    module docstring), so Open-Meteo's idea of "1 August" begins at 02:00 local in summer and
    01:00 in winter - asking for the dates verbatim would silently drop the first hours of
    the first day and add hours of the day after the last. Every date the caller types means
    a local date; that has to be true at the edges too, or a backfill and a re-backfill with
    different boundaries would disagree about two hours and nothing would say why.
    """
    # The trailing pad is clamped to today: the archive rejects an end_date in the future
    # outright ("out of allowed range"), so padding an up-to-today range would turn a valid
    # request into an HTTP 400. Nothing is lost - today's last UTC hour is already past
    # 23:00 local, which is as far as the trim reaches anyway.
    today = (datetime.now(ix.LOCAL_TZ) if ix.LOCAL_TZ else datetime.now()).date()
    end = min(_asDate(_shift(stop, 1)), today)
    payload = _get(ARCHIVE_URL, {"start_date": _date(_shift(start, -1)),
                                 "end_date": _date(end)})
    rows, nullHours, point = _parseHourly(payload)
    return _trim(rows, start, stop), nullHours, point


def fetchForecastPast(start, stop, today):
    """The same local date range, but from the FORECAST endpoint's past_days window.

    The two endpoints are not the same model. Checked on 2026-08-02, the archive resolved
    to grid point 52.337,5.167 and the forecast to 52.366,5.22 - ERA5 reanalysis against the
    operational forecast, on different grids. For most purposes that difference is smaller
    than the weather, but not for the one purpose this data exists to serve: a coefficient
    fitted on one model and applied to the other carries the difference between them,
    invisibly and for ever.

    So where the history is recent enough to be inside past_days - roughly 90 days, which
    covers every hour of measured load this house has - it can be fetched from the same
    model capture_weather.py stores forward. Beyond that only the archive reaches, and the
    seam between the two is a real one that any long fit has to acknowledge.
    """
    pastDays = (today - _asDate(start)).days + 1
    if pastDays < 0:
        pastDays = 0
    rows, nullHours, point = fetchForecast(pastDays=min(pastDays, 92), forecastDays=2)
    return _trim(rows, start, stop), nullHours, point


def _trim(rows, start, stop):
    first, last = _date(start), _date(stop)
    return [r for r in rows if first <= r[0].strftime("%Y-%m-%d") <= last]


def _shift(value, days):
    return value + timedelta(days=days)


def _asDate(value):
    return value.date() if isinstance(value, datetime) else value


def fetchForecast(pastDays=0, forecastDays=3):
    """Forecast hours, optionally with recent past ones from the same model run."""
    return _parseHourly(_get(FORECAST_URL, {"past_days": int(pastDays),
                                            "forecast_days": int(forecastDays)}))


def _date(value):
    return value.strftime("%Y-%m-%d")


def linesFor(rows, measurement, run=None):
    """Line protocol for a parsed fetch. `run` tags a forecast with when it was made.

    The observed series carries no run tag on purpose, so re-running a backfill overwrites
    in place - InfluxDB dedupes on measurement+tags+timestamp+field - and the script is
    idempotent by construction rather than by the caller remembering.

    The forecast series does carry one, and therefore adds a series per run for ever. That
    is the cardinality trap report_day.py's scoreLines() refuses to walk into, accepted here
    because without it the data answers nothing: a forecast with no record of when it was
    made cannot be scored against what happened, which is the only reason to keep it.
    """
    tags = {"source": SOURCE_TAG}
    if run is not None:
        tags["weather_run"] = run
    return [ix.linePoint(measurement, tags, fields, when) for when, fields in rows]


def runStamp(when=None):
    """The fetch instant as UTC ISO with a Z, matching how plans stamp plan_run."""
    when = when or datetime.now(timezone.utc)
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def describe(rows, nullHours, point):
    """The two lines every caller prints: where the data is from, and what came back."""
    c = config()
    out = ["site %.4f,%.4f -> grid point %s,%s"
           % (c["lat"], c["lon"], point[0], point[1])]
    if rows:
        out.append("%d hours, %s -> %s, %d empty hours skipped"
                   % (len(rows), rows[0][0].strftime("%Y-%m-%d %H:%M"),
                      rows[-1][0].strftime("%Y-%m-%d %H:%M"), nullHours))
    else:
        out.append("no hours returned (%d empty)" % nullHours)
    return out
