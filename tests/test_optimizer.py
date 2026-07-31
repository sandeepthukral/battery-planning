"""LPoptimization() in isolation - no subprocess, no network, no CLI.

Unlocked by CODE-REVIEW.md's A2 step 1: LPoptimization() now takes explicit
parameters (defaulting to the module globals the live path still uses), so a test
can hand it a small hand-built priceList and assert on the schedule directly instead
of driving the whole CLI through a fixture.

Row shape, matching Marstek-planning.py's own priceList convention:
    [seqNr, price_kwh, utc_str, local_str, pvDirect, pvIndirect, load, priceBuy, priceSell]
Indices 4-8 are the ones LPoptimization() reads (forecastDirectIndex .. sellPriceIndex).
This installation has no direct-coupled group, so pvDirect (index 4) is always 0 - see
the pvGroups comment block at the top of Marstek-planning.py.
"""
import pytest

# `planner` fixture (loads Marstek-planning.py fresh per test) comes from conftest.py.


def _row(seq, priceBuy, priceSell, pvIndirect=0, load=0, localTime="2026-01-01 %02d:00" % 0):
    return [seq, priceBuy, "2026-01-01 %02d:00" % seq, localTime, 0, pvIndirect, load, priceBuy, priceSell]


def _flatPriceRow(seq, price, load=0):
    return _row(seq, price, price, load=load)


COMMON = dict(
    ratedBatteryCapacity=27900, maxChargeSpeed=4850, maxDischargeSpeed=4700,
    minBatterySOCPct=10, onewayEff=0.9487, cycleCosts=0.0451,
    hourAvgPlanning=True, gridConnectionLimit=8050, gridLimitAppliesToExport=True,
    zeroGridCharge=False, terminalReserveWh=0,   # 0, not None: opt out of calcTerminalReserveWh
)


def test_flat_prices_no_arbitrage_cycling(planner):
    """Buying and selling at the same price only loses money to cycleCosts, so the
    optimiser should not cycle the battery at all - this is the simplest possible
    check that the objective (and cycleCosts) still does what it says.

    initialCharge is pinned to the SOC floor deliberately: starting with spare stored
    energy gives the optimiser a reason to discharge regardless of price, since with
    terminalReserveWh=0 unused energy has no future value - see
    test_without_reserve_it_sells_down_to_the_floor for that behaviour on its own.
    Starting at the floor isolates the thing this test actually checks: whether flat
    prices create a profitable charge-then-discharge PAIR. They should not."""
    priceList = [_flatPriceRow(t, 0.20, load=500) for t in range(6)]
    status, schedule = planner.LPoptimization(
        priceList=priceList, initialCharge=2790, **COMMON)
    assert status == "Optimal"
    assert all(r["charge"] == 0 and r["discharge"] == 0 for r in schedule)


def test_cheap_then_dear_charges_then_discharges(planner):
    """One cheap hour, one dear hour: the plan should charge in the cheap one and
    discharge in the dear one - the most basic arbitrage behaviour there is.

    initialCharge is pinned near the SOC floor for the same reason as the test
    above: starting with plenty of spare charge lets the optimiser sell in BOTH
    intervals without ever needing to charge first (dumping pre-existing energy is
    profitable at any positive price once terminalReserveWh is 0), which would make
    this pass for the wrong reason. Starting near-empty forces it to actually charge
    before it can discharge in the dear interval."""
    priceList = [
        _row(0, 0.05, 0.05, load=0),   # cheap
        _row(1, 0.40, 0.40, load=0),   # dear
    ]
    status, schedule = planner.LPoptimization(
        priceList=priceList, initialCharge=2800, **COMMON)
    assert status == "Optimal"
    assert schedule[0]["charge"] > 0
    assert schedule[1]["discharge"] > 0


def test_terminal_reserve_is_enforced(planner):
    """Passing terminalReserveWh directly (rather than relying on
    calcTerminalReserveWh(), which is A2 step 2's job) proves the constraint itself
    - sockWh[-1] >= terminalReserveWh - is wired up correctly."""
    priceList = [_row(t, 0.40, 0.40, load=0) for t in range(4)]  # all dear: sell everything it can
    args = dict(COMMON, terminalReserveWh=10000)
    status, schedule = planner.LPoptimization(
        priceList=priceList, initialCharge=14000, **args)
    assert status == "Optimal"
    assert schedule[-1]["soc"] >= 10000


def test_without_reserve_it_sells_down_to_the_floor(planner):
    """The regression test for the bug the reserve was built to fix: with no
    reserve requested, every dear-priced interval it can reach gets sold, and the
    plan ends at the SOC floor rather than holding anything back."""
    priceList = [_row(t, 0.40, 0.40, load=0) for t in range(4)]
    status, schedule = planner.LPoptimization(
        priceList=priceList, initialCharge=14000, **COMMON)  # terminalReserveWh=0
    assert status == "Optimal"
    floorWh = int(0.10 * 27900)
    assert schedule[-1]["soc"] <= floorWh + 1   # +1: integer rounding in the schedule dict


def test_initial_charge_below_floor_still_solves(planner):
    """If the battery is actually below the SOC floor when planning starts, the
    plan must still solve (interval 0's floor is relaxed to reality) rather than
    coming back infeasible exactly when a plan is most needed."""
    lowFloorArgs = dict(COMMON, minBatterySOCPct=20)   # floor 5580 Wh
    priceList = [_row(t, 0.20, 0.20, load=0) for t in range(3)]
    status, schedule = planner.LPoptimization(
        priceList=priceList, initialCharge=1000, **lowFloorArgs)  # below the 5580 Wh floor
    assert status == "Optimal"


def test_grid_connection_limit_caps_import(planner):
    """gridConnectionLimit bounds the METER, not the battery - a large load plus a
    cheap-hour charge should be capped at the fuse rather than left unbounded."""
    args = dict(COMMON, gridConnectionLimit=2000, hourAvgPlanning=True)
    priceList = [_row(0, 0.05, 0.05, load=6000)]  # load alone would want > 2000 W
    status, schedule = planner.LPoptimization(
        priceList=priceList, initialCharge=14000, **args)
    assert status == "Optimal"
    assert schedule[0]["import"] <= 2000


def test_zero_grid_charge_forbids_all_import(planner):
    args = dict(COMMON, zeroGridCharge=True)
    priceList = [_row(t, 0.05, 0.05, load=1000) for t in range(3)]  # cheap, would love to import
    status, schedule = planner.LPoptimization(
        priceList=priceList, initialCharge=14000, **args)
    assert status == "Optimal"
    assert all(r["import"] == 0 for r in schedule)


def test_energy_balance_holds_every_interval(planner):
    """pv + import + discharge == load + export + charge, for every interval -
    the LP's own constraint, checked independently rather than trusted."""
    priceList = [_row(t, 0.10 + 0.05 * (t % 3), 0.10, pvIndirect=800, load=500) for t in range(8)]
    status, schedule = planner.LPoptimization(
        priceList=priceList, initialCharge=14000, **COMMON)
    assert status == "Optimal"
    for t, r in enumerate(schedule):
        pv, load = priceList[t][5], priceList[t][6]
        lhs = pv + r["import"] + r["discharge"]
        rhs = load + r["export"] + r["charge"]
        assert abs(lhs - rhs) <= 1, "interval %d: %d != %d" % (t, lhs, rhs)


def test_refuses_empty_price_list(planner):
    """CODE-REVIEW.md C1b: an empty priceList used to solve as "Optimal" with an empty
    schedule, indistinguishable from a healthy short plan. It must refuse instead."""
    with pytest.raises(SystemExit) as excinfo:
        planner.LPoptimization(priceList=[], initialCharge=14000, **COMMON)
    assert excinfo.value.code == 5


def test_live_path_unaffected_by_default_arguments(planner):
    """Calling with every argument omitted must read the module globals exactly as
    it did before A2 step 1 - the live path (main() calls LPoptimization() with no
    arguments) must not change behaviour."""
    planner.priceList = [_row(t, 0.10, 0.10, load=300) for t in range(4)]
    planner.initialCharge = 14000
    planner.ratedBatteryCapacity = 27900
    planner.maxChargeSpeed = 4850
    planner.maxDischargeSpeed = 4700
    planner.minBatterySOCPct = 10
    planner.onewayEff = 0.9487
    planner.cycleCosts = 0.0451
    planner.hourAvgPlanning = True
    planner.gridConnectionLimit = 8050
    planner.gridLimitAppliesToExport = True
    planner.zeroGridCharge = False
    planner.useTerminalReserve = False   # so calcTerminalReserveWh() returns 0 with no other setup

    status, schedule = planner.LPoptimization()   # every argument defaults to None
    assert status == "Optimal"
    assert len(schedule) == 4
