"""influx_source.config() caching, and resetConfig() (CODE-REVIEW.md A3).

Without resetConfig(), config() computes its answer once and caches it forever - the
right behaviour for a real process (reads its environment once at startup) and the
wrong one for a test process, where the next test's os.environ changes would
otherwise be invisible.
"""
import os
import sys

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
