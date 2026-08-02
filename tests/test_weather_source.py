"""weather_source.py's parsing and line building, with requests.get monkeypatched.

No test here reaches the network. The response shapes below were taken from real
Open-Meteo replies on 2026-08-02 and trimmed, so a change in the API's field names
breaks these tests for the same reason it would break the script.
"""
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import influx_source as ix
import weather_source as ws


def _payload(times, **series):
    """An Open-Meteo response. Any variable not named is filled with a constant."""
    hourly = {"time": list(times)}
    for name, _field, _factor in ws.VARIABLES:
        hourly[name] = series.get(name, [1.0] * len(times))
    return {"latitude": 52.38, "longitude": 5.22, "hourly": hourly}


class _Response:
    def __init__(self, payload, status=200, text=""):
        self._payload = payload
        self.status_code = status
        self.text = text

    def json(self):
        return self._payload


def _fakeGet(monkeypatch, response, captured=None):
    def fake(url, params=None, timeout=None):
        if captured is not None:
            captured.append((url, params))
        return response
    monkeypatch.setattr(ws.requests, "get", fake)


# --- parsing --------------------------------------------------------------------------

def test_utc_hours_become_local_instants():
    """The API is asked in UTC and answers in UTC; the rows come back local. In August
    Amsterdam is UTC+2, so 12:00Z is 14:00 local."""
    rows, _, _ = ws._parseHourly(_payload(["2026-08-02T12:00"]))
    expected = 14 if ix.LOCAL_TZ else 12
    assert rows[0][0].hour == expected


def test_the_request_asks_for_utc_and_metres_per_second(monkeypatch):
    """Both are deliberate. Local time would give two indistinguishable 02:00s on the
    October transition, and Open-Meteo's default wind unit is km/h, which would land in a
    field called wind_ms."""
    captured = []
    _fakeGet(monkeypatch, _Response(_payload(["2026-08-02T12:00"])), captured)
    ws.fetchForecast()
    _, params = captured[0]
    assert params["timezone"] == "UTC"
    assert params["wind_speed_unit"] == "ms"


def test_every_variable_is_renamed_to_carry_its_unit():
    rows, _, _ = ws._parseHourly(_payload(["2026-08-02T12:00"], temperature_2m=[21.5]))
    fields = rows[0][1]
    assert fields["temperature_c"] == 21.5
    assert set(fields) == {"temperature_c", "cloud_cover_pct", "radiation_w_m2",
                           "wind_ms", "humidity_pct"}


def test_an_entirely_empty_hour_is_dropped_and_counted():
    """The forecast API's past_days window reaches further back than it holds data for,
    so leading nulls are normal rather than an error - but they must not be written as
    zeroes, which would read as a freezing, windless, pitch-dark hour."""
    payload = _payload(["2026-08-02T00:00", "2026-08-02T01:00"])
    for name, _f, _x in ws.VARIABLES:
        payload["hourly"][name][0] = None
    rows, nullHours, _ = ws._parseHourly(payload)
    assert nullHours == 1
    assert len(rows) == 1
    assert rows[0][0].strftime("%H:%M") == ("03:00" if ix.LOCAL_TZ else "01:00")


def test_an_hour_missing_only_some_variables_keeps_the_rest():
    """Written narrower rather than not at all: linePoint drops None fields, so a
    radiation outage must not take the temperature down with it."""
    payload = _payload(["2026-08-02T12:00"], shortwave_radiation=[None])
    rows, nullHours, _ = ws._parseHourly(payload)
    assert nullHours == 0
    assert "radiation_w_m2" not in rows[0][1]
    assert "temperature_c" in rows[0][1]


def test_the_resolved_grid_point_is_returned_not_the_requested_one():
    """Open-Meteo snaps to its grid. Reporting what it actually answered for is the only
    way to notice a coordinate typo that still returns plausible weather."""
    _, _, point = ws._parseHourly(_payload(["2026-08-02T12:00"]))
    assert point == (52.38, 5.22)


def test_a_response_with_no_hourly_block_is_empty_not_an_exception():
    rows, nullHours, point = ws._parseHourly({"error": True, "reason": "no"})
    assert rows == [] and nullHours == 0 and point == (None, None)


# --- transport ------------------------------------------------------------------------

def test_an_http_error_carries_the_body_into_the_message(monkeypatch):
    _fakeGet(monkeypatch, _Response(None, status=400, text="Data corrupted at path lat"))
    try:
        ws.fetchForecast()
    except RuntimeError as exc:
        assert "400" in str(exc) and "Data corrupted" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_archive_asks_for_a_day_either_side(monkeypatch):
    """The request is in UTC, so Open-Meteo's "17 July" starts at 02:00 local in summer.
    Padding and trimming is what makes the dates the caller types mean local dates."""
    captured = []
    _fakeGet(monkeypatch, _Response(_payload([])), captured)
    ws.fetchArchive(datetime(2026, 7, 1), datetime(2026, 7, 10))
    _, params = captured[0]
    assert params["start_date"] == "2026-06-30"
    assert params["end_date"] == "2026-07-11"


def test_the_trailing_pad_never_asks_the_archive_for_tomorrow(monkeypatch):
    """The archive rejects a future end_date outright, so padding a range that ends today
    would turn a valid request into an HTTP 400 - which is exactly what it did the first
    time this ran against the real API."""
    captured = []
    _fakeGet(monkeypatch, _Response(_payload([])), captured)
    today = (datetime.now(ix.LOCAL_TZ) if ix.LOCAL_TZ else datetime.now()).date()
    ws.fetchArchive(today, today)
    assert captured[0][1]["end_date"] == today.strftime("%Y-%m-%d")


def test_archive_trims_back_to_the_local_dates_asked_for(monkeypatch):
    """The padding must not reach the caller: an hour outside the requested local dates is
    dropped, including the ones the UTC offset drags in at each end."""
    times = ["2026-07-31T22:00",   # 00:00 local on 1 Aug in summer - wanted
             "2026-07-31T21:00",   # 23:00 local on 31 Jul - padding, must go
             "2026-08-01T21:00",   # 23:00 local on 1 Aug - wanted
             "2026-08-01T23:00"]   # 01:00 local on 2 Aug - padding, must go
    _fakeGet(monkeypatch, _Response(_payload(times)))
    rows, _, _ = ws.fetchArchive(datetime(2026, 8, 1), datetime(2026, 8, 1))
    if ix.LOCAL_TZ:
        assert [r[0].strftime("%Y-%m-%d %H:%M") for r in rows] == ["2026-08-01 00:00",
                                                                   "2026-08-01 23:00"]


# --- line protocol --------------------------------------------------------------------

def test_observed_lines_carry_no_run_tag():
    """This is what makes the backfill idempotent - InfluxDB dedupes on
    measurement+tags+timestamp+field, so a second run overwrites rather than accumulates."""
    rows, _, _ = ws._parseHourly(_payload(["2026-08-02T12:00"]))
    line = ws.linesFor(rows, ws.MEASUREMENT_OBSERVED)[0]
    assert line.startswith("weather_observed,source=open-meteo ")
    assert "weather_run" not in line


def test_forecast_lines_carry_the_run_tag():
    rows, _, _ = ws._parseHourly(_payload(["2026-08-02T12:00"]))
    line = ws.linesFor(rows, ws.MEASUREMENT_FORECAST, run="2026-08-02T09:05:00Z")[0]
    assert "weather_run=2026-08-02T09:05:00Z" in line


def test_run_stamp_is_utc_with_a_z():
    """Matching how Marstek-planning.py stamps plan_run. A local stamp with a Z suffix
    parses two hours late in summer, which is how the load-profile tests first failed."""
    stamp = ws.runStamp(datetime(2026, 8, 2, 12, 0, tzinfo=ix.LOCAL_TZ or timezone.utc))
    assert stamp.endswith("Z")
    assert stamp == ("2026-08-02T10:00:00Z" if ix.LOCAL_TZ else "2026-08-02T12:00:00Z")


def test_config_rejects_a_non_numeric_coordinate(monkeypatch):
    monkeypatch.setenv("WEATHER_LAT", "almere")
    try:
        ws.config()
    except RuntimeError as exc:
        assert "WEATHER_LAT" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_config_defaults_to_almere(monkeypatch):
    monkeypatch.delenv("WEATHER_LAT", raising=False)
    monkeypatch.delenv("WEATHER_LON", raising=False)
    monkeypatch.setenv("INFLUX_ENV_FILE", "/nonexistent/.env")
    c = ws.config()
    assert (round(c["lat"], 3), round(c["lon"], 3)) == (52.371, 5.216)
