"""influx_source.config() caching, and resetConfig() (CODE-REVIEW.md A3).

Without resetConfig(), config() computes its answer once and caches it forever - the
right behaviour for a real process (reads its environment once at startup) and the
wrong one for a test process, where the next test's os.environ changes would
otherwise be invisible.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import influx_source as ix


def test_reset_config_forgets_the_cache(monkeypatch):
    monkeypatch.setenv("INFLUX_URL", "http://first:8086")
    monkeypatch.setenv("INFLUX_TOKEN", "first-token")
    ix.resetConfig()
    first = ix.config()
    assert first["url"] == "http://first:8086"

    monkeypatch.setenv("INFLUX_URL", "http://second:8086")
    monkeypatch.setenv("INFLUX_TOKEN", "second-token")
    # No resetConfig() yet: must still see the FIRST answer, proving config() really
    # does cache (this is what makes the next assertion meaningful).
    assert ix.config()["url"] == "http://first:8086"

    ix.resetConfig()
    second = ix.config()
    assert second["url"] == "http://second:8086"
    assert second["token"] == "second-token"


def test_configured_reflects_reset(monkeypatch):
    monkeypatch.delenv("INFLUX_URL", raising=False)
    monkeypatch.delenv("INFLUX_TOKEN", raising=False)
    monkeypatch.delenv("INFLUX_TOKEN_PLANNING", raising=False)
    monkeypatch.setenv("INFLUX_ENV_FILE", "/nonexistent/.env")
    ix.resetConfig()
    assert ix.configured() is False

    monkeypatch.setenv("INFLUX_URL", "http://influxdb:8086")
    monkeypatch.setenv("INFLUX_TOKEN", "a-token")
    ix.resetConfig()
    assert ix.configured() is True


# --- hourlyAvgProfileWh(): CODE-REVIEW.md C4 -----------------------------------------


def test_profile_window_spans_exactly_days_complete_days(monkeypatch):
    """Reproduces the influxProfileDays=7-returns-8-days item in TODO.md: the window
    used to snap only the HOUR, not the day, so it spanned days+1 calendar dates
    whenever "now" wasn't exactly midnight. Snapping to local midnight fixes it -
    verified here by capturing the exact start/stop hourlyEnergyWh() is called with,
    rather than trusting the day_index count alone (which is what silently returned
    8 before, without any test catching it)."""
    captured = {}

    def fakeHourlyEnergyWh(field, start, stop, min_coverage=0.5, clamp_negative=False):
        captured["start"], captured["stop"] = start, stop
        return {}

    monkeypatch.setattr(ix, "hourlyEnergyWh", fakeHourlyEnergyWh)
    ix.hourlyAvgProfileWh(ix.FIELD_LOAD, days=7)

    start, stop = captured["start"], captured["stop"]
    assert stop - start == timedelta(days=7)
    assert (start.hour, start.minute, start.second, start.microsecond) == (0, 0, 0, 0)
    assert (stop.hour, stop.minute, stop.second, stop.microsecond) == (0, 0, 0, 0)
    # every hour-of-day bucket therefore gets fed by the same 7 calendar dates - not 8
    assert (stop.date() - start.date()).days == 7


def test_profile_excludes_todays_partial_data(monkeypatch):
    """stop is local midnight, i.e. the start of TODAY - today's own (partial, still
    accumulating) hours must never enter the average, or the same unevenness this
    fix removes would come back through the other end of the window."""
    captured = {}

    def fakeHourlyEnergyWh(field, start, stop, min_coverage=0.5, clamp_negative=False):
        captured["stop"] = stop
        return {}

    monkeypatch.setattr(ix, "hourlyEnergyWh", fakeHourlyEnergyWh)
    ix.hourlyAvgProfileWh(ix.FIELD_LOAD, days=7)

    now = datetime.now(ix.LOCAL_TZ) if ix.LOCAL_TZ else datetime.now(timezone.utc)
    assert captured["stop"].date() == now.date()
    assert captured["stop"] <= now



NOW = datetime(2026, 9, 29, 14, 0, tzinfo=timezone.utc)


def _row(value, minutes_ago):
    t = (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [{"_time": t, "_value": str(value)}]


@pytest.fixture
def soc_sources(monkeypatch):
    """Point `_query` at canned rows per (measurement, field); returns the Flux it was sent."""
    monkeypatch.setenv("INFLUX_ENV_FILE", "/nonexistent/.env")
    monkeypatch.setenv("ALPHAESS_SYS_SN", "SN-TEST")
    ix.resetConfig()
    sent = []

    def install(cloud_rows, modbus_rows):
        answers = {(ix.MEASUREMENT, ix.FIELD_SOC): cloud_rows,
                   (ix.DISPATCH_MEASUREMENT, ix.DISPATCH_FIELD_SOC): modbus_rows}

        def fake(flux):
            sent.append(flux)
            for (measurement, field), rows in answers.items():
                if '"%s"' % measurement in flux and '"%s"' % field in flux:
                    return rows
            raise AssertionError("unexpected query: %s" % flux)
        monkeypatch.setattr(ix, "_query", fake)
        return sent
    yield install
    ix.resetConfig()


def test_a_fresh_cloud_reading_wins_without_asking_the_dispatcher(soc_sources):
    sent = soc_sources(_row(55.2, 1), _row(54.8, 0))
    assert ix.latestSocPercent(now=NOW) == 55.2
    assert len(sent) == 1


def test_falls_back_to_the_dispatcher_when_the_cloud_is_silent(soc_sources):
    """2026-09-29: the AlphaESS API down, `power_readings` empty, the planner refusing to plan
    while the dispatcher read the inverter's SoC every minute."""
    soc_sources([], _row(54.8, 1))
    assert ix.latestSocPercent(now=NOW) == 54.8


def test_a_stale_cloud_reading_loses_to_a_fresher_dispatcher_one(soc_sources):
    """The start of an outage: the last cloud sample is still inside the 30-minute window,
    but a battery charging at ~5 kW has moved ~2 kWh since."""
    soc_sources(_row(40.0, 25), _row(47.5, 1))
    assert ix.latestSocPercent(now=NOW) == 47.5


def test_a_stale_cloud_reading_still_beats_an_older_dispatcher_one(soc_sources):
    soc_sources(_row(40.0, 10), _row(38.0, 20))
    assert ix.latestSocPercent(now=NOW) == 40.0


def test_a_stale_cloud_reading_is_used_when_the_dispatcher_is_silent(soc_sources):
    soc_sources(_row(40.0, 10), [])
    assert ix.latestSocPercent(now=NOW) == 40.0


def test_none_when_both_are_silent(soc_sources):
    soc_sources([], [])
    assert ix.latestSocPercent(now=NOW) is None


def test_both_queries_carry_the_sys_sn_filter_and_the_same_window(soc_sources):
    sent = soc_sources([], _row(54.8, 1))
    ix.latestSocPercent(within_minutes=30, now=NOW)
    assert len(sent) == 2
    for flux in sent:
        assert 'r.sys_sn == "SN-TEST"' in flux
        assert "range(start: -30m)" in flux
