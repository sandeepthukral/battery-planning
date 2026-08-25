"""soc_curve replaces a linear Wh<->% conversion, so the properties that matter are
that it is a bijection, that it is monotonic, and that it still describes this battery.

The point of the curve is that it is *not* flat, so there is no single number to pin.
What is worth pinning is the shape that made it worth having: more Wh per point at the
top of the gauge than at the bottom. A refit that lost that shape would silently undo
the fix while every other test still passed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hardware
import soc_curve


def test_endpoints_are_the_ends_of_the_range():
    assert soc_curve.wh_at_pct(0) == 0.0
    assert soc_curve.wh_at_pct(100) == soc_curve.CAPACITY_WH
    assert soc_curve.pct_at_wh(0) == 0.0
    assert soc_curve.pct_at_wh(soc_curve.CAPACITY_WH) == 100.0


def test_round_trips_both_ways():
    for pct in range(0, 101):
        assert abs(soc_curve.pct_at_wh(soc_curve.wh_at_pct(pct)) - pct) < 1e-6
    step = soc_curve.CAPACITY_WH / 50.0
    for i in range(51):
        wh = i * step
        assert abs(soc_curve.wh_at_pct(soc_curve.pct_at_wh(wh)) - wh) < 1e-6


def test_is_strictly_increasing():
    # A flat or falling stretch would make pct_at_wh ambiguous, and would let the
    # planner discharge into a band that costs it no SoC at all.
    previous = -1.0
    for tenth in range(0, 1001):
        wh = soc_curve.wh_at_pct(tenth / 10.0)
        assert wh > previous
        previous = wh


def test_clamps_outside_the_range():
    assert soc_curve.wh_at_pct(-5) == 0.0
    assert soc_curve.wh_at_pct(150) == soc_curve.CAPACITY_WH
    assert soc_curve.pct_at_wh(-1) == 0.0
    assert soc_curve.pct_at_wh(soc_curve.CAPACITY_WH * 2) == 100.0


def test_every_band_is_a_plausible_density():
    # Loose bound. The measured spread is roughly 180-425 Wh/point; anything outside
    # this is a paste error or a fit that diverged, not a battery.
    assert len(soc_curve.WH_PER_PCT) == int(100 / soc_curve.BAND_WIDTH_PCT)
    for density in soc_curve.WH_PER_PCT:
        assert 100.0 < density < 600.0


def test_the_top_of_the_gauge_holds_more_than_the_bottom():
    # This is the whole reason the curve exists: 90-100% is worth substantially more
    # per point than 20-30%, which a single constant cannot express. Discharging
    # 90->80 must release clearly more energy than discharging 30->20.
    top = soc_curve.wh_at_pct(90) - soc_curve.wh_at_pct(80)
    bottom = soc_curve.wh_at_pct(30) - soc_curve.wh_at_pct(20)
    assert top > bottom * 1.3


def test_full_range_stays_near_the_rated_capacity():
    # The curve redistributes energy across the gauge rather than inventing it, so its
    # 0-100% total should stay in the same neighbourhood as the rated figure. A large
    # gap means the fit drifted, not that the battery grew.
    assert 0.8 < soc_curve.CAPACITY_WH / hardware.CAPACITY_WH < 1.25
