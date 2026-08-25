"""http_config.py is the single source for the outbound HTTP policy - timeouts (E7) and,
since a DNS blip cost a whole plan run, retries (E9).

Before E7, planner.py declared (10, 30) once and influx_source.py separately
hardcoded a bare 30 at its two live call sites.
"""
import datetime
import json
import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import http_config
import influx_source as ix


def test_timeout_is_a_connect_read_pair():
    assert http_config.HTTP_TIMEOUT == (10, 30)


def test_influx_source_reads_the_shared_timeout():
    assert ix.http_config.HTTP_TIMEOUT is http_config.HTTP_TIMEOUT


def test_planner_reads_the_shared_timeout(planner):
    assert planner.HTTP_TIMEOUT == http_config.HTTP_TIMEOUT


# --- retries (CODE-REVIEW.md E9) ---------------------------------------------------------
#
# The incident these cover: on 2026-08-18 the 13:05 run - the first of the day that can see
# tomorrow's day-ahead prices - died on `Temporary failure in name resolution` thirteen
# seconds after starting, and the plan stayed two hours stale until the next firing.


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


@pytest.fixture
def noSleep(monkeypatch):
    """Record what the ladder WOULD have slept, without spending it."""
    slept = []
    monkeypatch.setattr(http_config.time, "sleep", slept.append)
    return slept


def fakeGet(monkeypatch, *outcomes):
    """Queue one outcome per call: a FakeResponse to return, or an exception to raise."""
    calls = []

    def get(url, timeout=None):
        calls.append(url)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(http_config.requests, "get", get)
    return calls


def test_the_ladder_spends_about_a_minute():
    # The number the policy is actually chosen for. Sleeping less than this does not outlast
    # a DHCP renewal or a router reboot, which is the whole failure being defended against.
    assert sum(http_config.HTTP_RETRY_BACKOFF_S) == 50
    assert http_config.HTTP_RETRY_BACKOFF_S[0] >= 5, "a DNS failure returns instantly; an " \
        "immediate first retry just asks the same broken resolver the same question"


def test_a_dns_failure_is_retried_then_succeeds(monkeypatch, noSleep):
    boom = requests.exceptions.ConnectionError("Temporary failure in name resolution")
    calls = fakeGet(monkeypatch, boom, boom, FakeResponse(200, "prices"))
    response = http_config.getWithRetries("https://example.invalid/prices", "EnergyZero")
    assert response.text == "prices"
    assert len(calls) == 3
    assert noSleep == [5, 15]


def test_a_persistent_failure_still_raises(monkeypatch, noSleep):
    boom = requests.exceptions.ConnectionError("name resolution")
    calls = fakeGet(monkeypatch, boom, boom, boom, boom)
    with pytest.raises(requests.exceptions.ConnectionError):
        http_config.getWithRetries("https://example.invalid/", "EnergyZero")
    # Bounded: four attempts, not forever. The lock in scripts/plan.sh goes stale at 20
    # minutes and the schedule fires again in 60, so a run must never sit here indefinitely.
    assert len(calls) == 1 + len(http_config.HTTP_RETRY_BACKOFF_S)
    assert noSleep == list(http_config.HTTP_RETRY_BACKOFF_S)


def test_a_rate_limit_is_not_retried(monkeypatch, noSleep):
    # 429 is forecast.solar's ~12-per-hour free tier. Its window is an hour, so no ladder
    # measured in seconds can clear it, and each retry spends budget the NEXT run needs.
    # loadPVforecastIntoFile decodes the body to say so; it must still get to see it.
    calls = fakeGet(monkeypatch, FakeResponse(429))
    response = http_config.getWithRetries("https://api.forecast.solar/x", "forecast.solar")
    assert response.status_code == 429
    assert len(calls) == 1
    assert noSleep == []


def test_a_client_error_is_not_retried(monkeypatch, noSleep):
    calls = fakeGet(monkeypatch, FakeResponse(404))
    assert http_config.getWithRetries("https://example.invalid/", "ENTSOE").status_code == 404
    assert len(calls) == 1


def test_a_server_error_is_retried_and_then_returned_not_raised(monkeypatch, noSleep):
    # Returned, not raised, so every call site keeps its existing
    # `if response.status_code == 200: ... else: <report it>` shape.
    calls = fakeGet(monkeypatch, FakeResponse(503), FakeResponse(503),
                    FakeResponse(503), FakeResponse(503))
    assert http_config.getWithRetries("https://example.invalid/", "ENTSOE").status_code == 503
    assert len(calls) == 4


def test_the_live_call_sites_all_go_through_the_retry_helper():
    # The three outbound calls on the plan's critical path. Anything left calling
    # requests.get directly still dies on the first hiccup, which is the bug.
    source = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "planner.py")).read()
    live = source.split("def loadPVforecastIntoFile")[1]
    assert "requests.get(url,timeout=HTTP_TIMEOUT)" not in live
    assert live.count("http_config.getWithRetries(") == 3


def test_energyzero_falls_back_to_cache_when_the_fetch_raises(planner, monkeypatch, tmp_path,
                                                              noSleep):
    """The incident itself: a raised ConnectionError must take the cache path.

    It already did when EnergyZero answered with a non-200 - one failure, two spellings, and
    only one of them survivable. This is the test that says they are the same failure.
    """
    # Naive, as buildInitialPlanningList() passes it - the function compares runDate against
    # a naive literal, so an aware one raises before it reaches the fetch at all.
    runDate = datetime.datetime.combine(planner.today, datetime.time(13, 5))
    cached = {"base": [{"start": planner.today.strftime("%Y-%m-%dT00:00:00Z"),
                        "price": {"value": 0.11}}]}
    cacheDir = tmp_path / "price_cache"
    cacheDir.mkdir()
    key = planner.today.strftime("%d%m%Y") + "_q"
    (cacheDir / (key + ".json")).write_text(json.dumps(cached))

    monkeypatch.setenv("BT_PRICE_CACHE", str(cacheDir))
    # Set, not monkeypatched: includeTax is assigned inside getUserInput(), so a freshly
    # exec'd module has no such attribute until the CLI has run. The planner fixture is
    # function-scoped, so writing to it cannot leak into another test.
    planner.includeTax = False
    monkeypatch.setattr(planner.http_config.requests, "get", _raiseDNS)

    result = planner.getPricesFromEnergyZero(runDate, False)

    assert result, "a DNS failure with a warm cache must still yield prices"
    assert result[0][1] == 0.11
    # And it did not give up after one look.
    assert noSleep == list(http_config.HTTP_RETRY_BACKOFF_S)


def _raiseDNS(url, timeout=None):
    raise requests.exceptions.ConnectionError(
        "Failed to resolve 'public.api.energyzero.nl' ([Errno -3] Temporary failure in "
        "name resolution)")


def test_a_failure_does_not_log_the_entsoe_token(monkeypatch, noSleep, capsys):
    # requests quotes the failing url - path and query - back inside the exception, and the
    # ENTSOE url carries securityToken= in its query. logs/plan_*.log would have collected it
    # on every blip.
    url = "https://web-api.tp.entsoe.eu/api?securityToken=s3cret&documentType=A44"
    boom = requests.exceptions.ConnectionError(
        "HTTPSConnectionPool(host='web-api.tp.entsoe.eu', port=443): Max retries exceeded "
        "with url: /api?securityToken=s3cret&documentType=A44")
    fakeGet(monkeypatch, boom, FakeResponse(200))
    http_config.getWithRetries(url, "ENTSOE")
    logged = capsys.readouterr().out
    assert "s3cret" not in logged
    assert "<redacted>" in logged
    assert "ENTSOE" in logged, "the label still has to say which endpoint failed"
