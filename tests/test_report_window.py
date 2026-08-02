"""report_window.py's attribution, called directly with hand-built rows.

The arithmetic here decides whether a drift gets blamed on the operator or on the optimiser,
which are opposite conclusions leading to opposite fixes. It needs no database - attribute()
takes plain row dicts - so it is tested the same way test_report_day_forecast.py tests
sectionForecast(), and kept apart from anything that talks to InfluxDB.

The central claim every test below leans on: the three components reconstruct the drift
exactly, because they are an algebraic rearrangement of the optimiser's own SoC recursion
rather than an estimate of it. A test suite that only checked the components individually
would pass while they silently stopped adding up.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import influx_source as ix
import report_window as rw

TZ = ix.LOCAL_TZ or timezone.utc
T0 = datetime(2026, 8, 2, 19, 0, tzinfo=TZ)
EFF = rw.ONEWAY_EFF


def _rows(specs):
    """specs: (planDischarge, actDischarge, planSocWh, actSocWh) per 15-minute interval."""
    out = []
    for n, (planDis, actDis, planSoc, actSoc) in enumerate(specs):
        out.append({"time": T0 + timedelta(minutes=15 * n),
                    "planDischarge": planDis, "actDischarge": actDis,
                    "planCharge": 0.0, "actCharge": 0.0,
                    "planSoc": planSoc, "actSoc": actSoc,
                    "planPv": 0.0, "actPv": 0.0})
    return out


def _closes(a):
    """The components account for the whole drift, with nothing hiding in the residual."""
    assert abs(a["energy"] + a["model"] + a["pv"] + a["rest"] - a["drift"]) < 1e-6


def test_two_intervals_are_the_minimum_and_one_is_not():
    assert rw.attribute(_rows([(1000, 1000, 20000, 20000)])) is None
    assert rw.attribute([]) is None


def test_a_missing_measured_soc_does_not_count_as_an_interval():
    rows = _rows([(1000, 1000, 20000, 20000), (1000, 1000, 18000, 18000)])
    rows[1]["actSoc"] = None
    assert rw.attribute(rows) is None


def test_a_battery_obeying_the_model_exactly_shows_no_drift():
    """The plan's own recursion, played back as reality: SoC falls by discharge/eff."""
    drop = 1000.0 / EFF
    rows = _rows([(0, 0, 20000, 20000),
                  (1000, 1000, 20000 - drop, 20000 - drop),
                  (1000, 1000, 20000 - 2 * drop, 20000 - 2 * drop)])
    a = rw.attribute(rows)
    assert abs(a["drift"]) < 1e-6
    assert abs(a["energy"]) < 1e-6
    assert abs(a["model"]) < 1e-6
    _closes(a)


def test_a_battery_that_moved_no_energy_is_all_energy_and_no_model():
    """A late start. The plan discharged, the battery did not, and its SoC held."""
    drop = 1000.0 / EFF
    rows = _rows([(0, 0, 20000, 20000),
                  (1000, 0, 20000 - drop, 20000),
                  (1000, 0, 20000 - 2 * drop, 20000)])
    a = rw.attribute(rows)
    assert abs(a["energy"] - 2 * drop) < 1e-6
    assert abs(a["model"]) < 1e-6
    _closes(a)


def test_matching_energy_with_a_shallower_soc_fall_is_all_model():
    """2026-08-02 19:30-22:15 in miniature: the battery moved exactly what the plan asked
    and lost less SoC doing it. Nothing operational to fix; the optimiser is wrong."""
    rows = _rows([(0, 0, 20000, 20000),
                  (1000, 1000, 20000 - 1000 / EFF, 19000),
                  (1000, 1000, 20000 - 2000 / EFF, 18000)])
    a = rw.attribute(rows)
    assert abs(a["energy"]) < 1e-6
    assert abs(a["model"] - (2000.0 / EFF - 2000.0)) < 1e-6
    assert a["model"] > 0
    _closes(a)


def test_the_two_causes_are_separated_when_both_are_present():
    """Half the intervals missed entirely, the rest tracked with a shallower SoC fall.
    A report that lumped these together would blame the whole thing on whichever cause the
    reader already believed in."""
    rows = _rows([(0, 0, 20000, 20000),
                  (1000, 0, 20000 - 1000 / EFF, 20000),
                  (1000, 1000, 20000 - 2000 / EFF, 19000)])
    a = rw.attribute(rows)
    assert abs(a["energy"] - 1000.0 / EFF) < 1e-6
    assert abs(a["model"] - (1000.0 / EFF - 1000.0)) < 1e-6
    _closes(a)


def test_charging_is_attributed_the_same_way():
    """The plan credits eff*charged to SoC. A battery that banks the full amount is the same
    modelling error as the discharge case, with the sign the other way round - and it must
    land in MODEL, not be waved through because charging "looked fine"."""
    rows = _rows([(0, 0, 10000, 10000), (0, 0, 10000 + 1000 * EFF, 11000)])
    rows[1]["planCharge"], rows[1]["actCharge"] = 1000.0, 1000.0
    a = rw.attribute(rows)
    assert abs(a["energy"]) < 1e-6
    assert abs(a["model"] - (1000.0 - EFF * 1000.0)) < 1e-6
    assert a["drift"] > 0          # actual SoC rose further than planned: the gap opens
    _closes(a)


def test_forecast_pv_is_named_rather_than_left_unexplained():
    """The recursion credits forecast PV straight to the plan's SoC. Before this was a named
    component it landed in the residual, where it looked like the plan failing to satisfy its
    own arithmetic."""
    drop = 1000.0 / EFF - 200.0
    rows = _rows([(0, 0, 20000, 20000), (1000, 1000, 20000 - drop, 20000 - 1000)])
    rows[1]["planPv"] = 200.0
    a = rw.attribute(rows)
    assert abs(a["pv"] + 200.0) < 1e-6
    assert abs(a["rest"]) < 1e-6
    _closes(a)


def test_the_gap_at_each_end_is_reported_from_the_first_and_last_usable_rows():
    rows = _rows([(0, 0, 20000, 21000), (1000, 1000, 18000, 20000), (1000, 1000, 16000, 19000)])
    a = rw.attribute(rows)
    assert a["openGap"] == 1000
    assert a["closeGap"] == 3000
    assert a["from"] == T0
    assert a["to"] == T0 + timedelta(minutes=30)


def test_the_first_interval_contributes_no_energy():
    """It establishes the starting SoC, exactly as the recursion treats interval 0. Counting
    its discharge would charge the window for energy moved before the window opened."""
    rows = _rows([(9999, 9999, 20000, 20000), (1000, 1000, 19000, 19000)])
    a = rw.attribute(rows)
    assert a["planDischarge"] == 1000
    assert a["actDischarge"] == 1000


def test_oneway_eff_follows_the_planners_own_rte_derivation():
    """Same expression as Marstek-planning.py:545. If one moves and the other does not, this
    report silently starts measuring the drift against an efficiency the plan never used."""
    assert abs(rw.ONEWAY_EFF - (100.0 - (100.0 - rw.RTE) / 2.0) / 100.0) < 1e-12
