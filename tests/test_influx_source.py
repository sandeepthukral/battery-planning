"""influx_source.config() caching, and resetConfig() (CODE-REVIEW.md A3).

Without resetConfig(), config() computes its answer once and caches it forever - the
right behaviour for a real process (reads its environment once at startup) and the
wrong one for a test process, where the next test's os.environ changes would
otherwise be invisible.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

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


def _fake_query(answers, seen):
    """A `_query` stand-in: the first (measurement, field) pair named in the Flux picks the
    rows returned."""
    def fake(flux):
        for (measurement, field), rows in answers.items():
            if '"%s"' % measurement in flux and '"%s"' % field in flux:
                seen.append(measurement)
                return rows
        raise AssertionError("unexpected query: %s" % flux)
    return fake


def test_latest_soc_prefers_the_collectors_reading(monkeypatch):
    seen = []
    monkeypatch.setattr(ix, "_query", _fake_query({
        (ix.MEASUREMENT, ix.FIELD_SOC): [{"_value": "55.2"}],
        (ix.DISPATCH_MEASUREMENT, ix.DISPATCH_FIELD_SOC): [{"_value": "54.8"}],
    }, seen))
    assert ix.latestSocPercent() == 55.2
    assert seen == [ix.MEASUREMENT]


def test_latest_soc_falls_back_to_the_dispatcher_when_the_cloud_is_silent(monkeypatch):
    """2026-09-29: the AlphaESS API down, `power_readings` empty, the planner refusing to plan
    while the dispatcher read the inverter's SoC every minute."""
    seen = []
    monkeypatch.setattr(ix, "_query", _fake_query({
        (ix.MEASUREMENT, ix.FIELD_SOC): [],
        (ix.DISPATCH_MEASUREMENT, ix.DISPATCH_FIELD_SOC): [{"_value": "54.8"}],
    }, seen))
    assert ix.latestSocPercent() == 54.8
    assert seen == [ix.MEASUREMENT, ix.DISPATCH_MEASUREMENT]


def test_latest_soc_is_none_when_both_are_silent(monkeypatch):
    monkeypatch.setattr(ix, "_query", _fake_query({
        (ix.MEASUREMENT, ix.FIELD_SOC): [],
        (ix.DISPATCH_MEASUREMENT, ix.DISPATCH_FIELD_SOC): [],
    }, []))
    assert ix.latestSocPercent() is None
