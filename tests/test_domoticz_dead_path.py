"""The Domoticz functions (useDomoticz=False, dead by design - see the "no execution"
constraint) - CODE-REVIEW.md Stage 4: hardening code that doesn't run today but is kept
for later. Each of these calls a Domoticz-shaped requests function; monkeypatching
requests.get to raise reproduces exactly the case B3 fixes - a transport failure
(DNS, connection refused, timeout) before `response` is ever assigned.
"""
import pytest


class _Boom(Exception):
    pass


def _raisingGet(*args, **kwargs):
    raise _Boom("connection refused")


@pytest.mark.parametrize("call", [
    lambda p: p.getLocation(),
    lambda p: p.getUserVariable(1),
    lambda p: p.getPercentageDevice(1),
    lambda p: p.setTextDevice(1, "x"),
    lambda p: p.updatePowerDevice(1, 100),
    lambda p: p.getHourlyDataFromShortHistory(1),
    lambda p: p.updateSelectorSwitch(1, 10),
], ids=["getLocation", "getUserVariable", "getPercentageDevice", "setTextDevice",
        "updatePowerDevice", "getHourlyDataFromShortHistory", "updateSelectorSwitch"])
def test_transport_failure_before_response_is_bound_does_not_crash(planner, monkeypatch, call, capsys):
    """CODE-REVIEW.md B3: these used to reference `response` inside their own except
    block. If requests.get() itself raised - never returning a Response - `response`
    was never assigned, and the error handler crashed with UnboundLocalError instead
    of reporting the real error. useDomoticz has to be True for these functions to
    reach the network path at all; getLocation() short-circuits before that otherwise."""
    monkeypatch.setattr(planner, "useDomoticz", True)
    monkeypatch.setattr(planner.requests, "get", _raisingGet)
    result = call(planner)   # must not raise
    # setTextDevice/updatePowerDevice/updateSelectorSwitch return a plain bool; the
    # others return a (responseResult, value) tuple - either way, false/falsy on failure.
    ok = result[0] if isinstance(result, tuple) else result
    assert ok is False
    assert "connection refused" in capsys.readouterr().out


def test_domoticz_calls_pass_the_shared_timeout(planner, monkeypatch):
    """CODE-REVIEW.md B4: these used to have no timeout= at all - a scheduled job
    hanging forever on a dead Domoticz host is worse than one that fails quickly."""
    captured = {}

    def fakeGet(url, **kwargs):
        captured.update(kwargs)
        raise _Boom("stop here, we only want the call arguments")

    monkeypatch.setattr(planner, "useDomoticz", True)
    monkeypatch.setattr(planner.requests, "get", fakeGet)
    planner.getLocation()
    assert captured.get("timeout") == planner.HTTP_TIMEOUT


# --- setBatteryAction(): B4 (URL escaping, timeout) and B5 (no unconditional email) --


def _readySetBatteryAction(planner, monkeypatch, deviceUpdatesSucceed):
    """Common setup: clearTextDevice/setTextDevice/updatePowerDevice/
    updateSelectorSwitch stubbed out (each already has its own direct tests
    above) so only setBatteryAction()'s own logic is under test."""
    monkeypatch.setattr(planner, "hourAvgPlanning", True, raising=False)
    monkeypatch.setattr(planner, "maxChargeSpeed", 4850, raising=False)
    monkeypatch.setattr(planner, "maxDischargeSpeed", 4700, raising=False)
    monkeypatch.setattr(planner, "priceList", [], raising=False)
    monkeypatch.setattr(planner, "clearTextDevice", lambda idx: True)
    monkeypatch.setattr(planner, "setTextDevice", lambda idx, text: True)
    if deviceUpdatesSucceed:
        monkeypatch.setattr(planner, "updatePowerDevice", lambda idx, power: True)
        monkeypatch.setattr(planner, "updateSelectorSwitch", lambda idx, level: True)
    else:
        def _boom(*a, **k):
            raise _Boom("device update failed")
        monkeypatch.setattr(planner, "updatePowerDevice", _boom)


def test_no_notification_sent_when_the_action_attempt_failed(planner, monkeypatch):
    """CODE-REVIEW.md B5: this used to send a "success" notification unconditionally,
    even when the device-update block above it failed - actively misleading, not
    just wasted effort."""
    _readySetBatteryAction(planner, monkeypatch, deviceUpdatesSucceed=False)
    called = []
    monkeypatch.setattr(planner.requests, "get", lambda *a, **k: called.append(1))
    result = planner.setBatteryAction("Manual", "2026-01-01 10:00", 500, [])
    assert result is False
    assert called == [], "no notification should be sent when the action attempt failed"


def test_notification_sent_via_baseJSON_not_a_hardcoded_host(planner, monkeypatch):
    """CODE-REVIEW.md B5: this used to hardcode http://127.0.0.1:8080, ignoring
    domoticzIP/domoticzPort. baseJSON is built from those and used everywhere else."""
    _readySetBatteryAction(planner, monkeypatch, deviceUpdatesSucceed=True)
    captured = {}

    class _FakeResponse:
        def json(self):
            return {"status": "OK"}

    def fakeGet(url, **kwargs):
        captured["url"] = url
        captured["timeout"] = kwargs.get("timeout")
        return _FakeResponse()

    monkeypatch.setattr(planner.requests, "get", fakeGet)
    result = planner.setBatteryAction("Manual", "2026-01-01 10:00", 500, [])
    assert result is True
    assert captured["url"].startswith(planner.baseJSON)
    assert "127.0.0.1:8080" not in captured["url"]
    assert captured["timeout"] == planner.HTTP_TIMEOUT


def test_calc_hourly_avg_usage_returns_empty_list_on_domoticz_failure(planner, monkeypatch):
    """CODE-REVIEW.md C6: hourlyAvgs was only ever assigned inside `if responseResult:`.
    When getHourlyDataFromShortHistory() fails, the function used to reach
    `return responseResult, hourlyAvgs` with hourlyAvgs never bound - UnboundLocalError
    instead of the (False, []) shape its own sibling early-returns already use."""
    monkeypatch.setattr(planner, "useInflux", False)
    monkeypatch.setattr(planner, "useDomoticz", True)
    monkeypatch.setattr(planner, "getHourlyDataFromShortHistory", lambda varIDX: (False, None))
    result = planner.calcHourlyAvgUsage(1, 0.1)   # must not raise
    assert result == (False, [])


def test_notification_subject_and_body_are_url_escaped(planner, monkeypatch):
    """CODE-REVIEW.md B4: raw string interpolation into a URL is unsafe if the
    content contains characters like '&' - urllib.parse.quote() is what
    setTextDevice() already does for its own text."""
    _readySetBatteryAction(planner, monkeypatch, deviceUpdatesSucceed=True)
    captured = {}

    class _FakeResponse:
        def json(self):
            return {"status": "OK"}

    def fakeGet(url, **kwargs):
        captured["url"] = url
        return _FakeResponse()

    monkeypatch.setattr(planner.requests, "get", fakeGet)
    planner.setBatteryAction("Manual", "2026-01-01 10:00", 500, [])
    # the literal, unescaped action name must not appear raw in a way that could
    # break the query string - it is present only inside the quoted subject param
    assert "subject=BATTERY%3A" in captured["url"] or "subject=BATTERY%3a" in captured["url"].lower()
