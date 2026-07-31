"""hoursUntilRefill() and calcTerminalReserveWh() in isolation.

Unlocked by CODE-REVIEW.md's A2 step 2: both now take explicit parameters,
defaulting to the module globals the live path still uses.
"""

# --- hoursUntilRefill(): the asymmetry it exists for --------------------------------
# The cheapest hour of the day here is midday (solar glut) for two thirds of the year
# and pre-dawn only in Oct-Feb. hoursUntilRefill() must find the cheap hour wherever
# the window's own prices put it, not assume "cheap == overnight".


def _bandedPrices(cheapHours, cheap=0.05, dear=0.25):
    """24 hourly prices: `cheap` on the given hours, `dear` everywhere else.

    cheapQuantile is a QUANTILE (default 0.25, i.e. the cheapest quarter of the
    window's hours), not "below some fixed price" - a single outlier hour among 23
    identical dear ones does not clear a 25th-percentile filter, the 25th percentile
    of [dear]*23 + [outlier] is still `dear`. len(cheapHours) is deliberately >= 6
    (a quarter of 24) so the threshold genuinely lands inside the cheap band, the
    same shape real day-ahead prices have (a cheap PERIOD, not a single instant).
    """
    assert len(cheapHours) >= 6, "cheap band must be >= 25% of 24 hours to clear cheapQuantile"
    prices = [dear] * 24
    for h in cheapHours:
        prices[h] = cheap
    return prices


def test_sun_takes_over_ends_the_reserve_immediately(planner):
    loadAvg = [1.0] * 24
    pvAvg = [0.0] * 24
    pvAvg[8] = 2.0                                    # forecast PV exceeds load at 08:00
    priceAvg = _bandedPrices(cheapHours=range(14, 20))  # cheap band elsewhere (afternoon)
    counts = [4] * 24
    hours, reason = planner.hoursUntilRefill(6, 6, loadAvg, pvAvg, priceAvg, counts)
    assert hours == 2                                 # 06 -> 07 -> 08: sun wins before price is checked
    assert "sun" in reason


def test_cheap_hour_summer_midday(planner):
    loadAvg = [1.0] * 24
    pvAvg = [0.0] * 24
    priceAvg = _bandedPrices(cheapHours=range(10, 16))  # solar-glut midday low, July pattern
    counts = [4] * 24
    hours, reason = planner.hoursUntilRefill(8, 7, loadAvg, pvAvg, priceAvg, counts)
    assert hours == 2                                  # 08 -> 09 -> 10 (first hour of the band)
    assert "cheap hour" in reason


def test_cheap_hour_winter_predawn(planner):
    loadAvg = [1.0] * 24
    pvAvg = [0.0] * 24
    priceAvg = _bandedPrices(cheapHours=range(3, 9))    # pre-dawn/morning low, January pattern
    counts = [4] * 24
    hours, reason = planner.hoursUntilRefill(22, 1, loadAvg, pvAvg, priceAvg, counts)
    assert hours == 5                                   # 22 -> 23 -> 00 -> 01 -> 02 -> 03
    assert "cheap hour" in reason


def test_falls_back_to_typical_cheap_hour_when_window_has_no_prices(planner):
    """The window doesn't cover the refill hour at all (counts==0 everywhere): must
    fall back to typicalCheapHourByMonth, not stay silent or pick something arbitrary."""
    loadAvg = [1.0] * 24
    pvAvg = [0.0] * 24
    priceAvg = [None] * 24
    counts = [0] * 24
    hours, reason = planner.hoursUntilRefill(22, 1, loadAvg, pvAvg, priceAvg, counts)
    assert hours == 6                                  # typicalCheapHourByMonth[0] (Jan) == 4
    assert "typical cheap hour" in reason


def test_caps_at_reserveMaxHours(planner):
    loadAvg = [1.0] * 24
    pvAvg = [0.0] * 24
    priceAvg = [None] * 24
    counts = [0] * 24
    # July's typical cheap hour is 14:00 (typicalCheapHourByMonth[6]); reserveMaxHours=5
    # from startHour=0 never reaches it, so the cap must fire instead.
    hours, reason = planner.hoursUntilRefill(0, 7, loadAvg, pvAvg, priceAvg, counts,
                                              reserveMaxHours=5)
    assert hours == 5
    assert reason == "reserveMaxHours cap"


# --- calcTerminalReserveWh(): the end-to-end arithmetic ------------------------------


def _row(seq, priceBuy, priceSell, pvIndirect=0, load=0, localTime="2026-01-01 00:00"):
    return [seq, priceBuy, localTime, localTime, 0, pvIndirect, load, priceBuy, priceSell]


COMMON = dict(hourAvgPlanning=True)   # perHour=1 in hourlyShapeFromPriceList - no /4 scaling


def test_reserve_is_floor_only_when_window_ends_at_a_refill_opportunity(planner):
    """priceList[-1] sets the window end (20:00, so startHour=21). A separate row
    supplies hour 21's own forecast (pv exceeds load there), so hoursUntilRefill sees
    the sun taking over at step 0 and the reserve collapses to the floor."""
    priceList = [
        _row(0, 0.20, 0.20, pvIndirect=1500, load=500, localTime="2026-01-01 21:00"),
        _row(1, 0.20, 0.20, pvIndirect=0, load=500, localTime="2026-01-01 20:00"),  # LAST: window end
    ]
    reserveWh = planner.calcTerminalReserveWh(
        priceList=priceList, ratedBatteryCapacity=27900, reserveFloorPct=15, **COMMON)
    assert reserveWh == int(0.15 * 27900)


def test_reserve_covers_load_until_the_cheap_hour(planner):
    """No sun anywhere; hours 21 and 22 are dear, hour 23 is cheap. The reserve must
    cover the load in the two dear hours (21, 22) plus the configured margin - not
    just fall back to the floor, and not extend past the cheap hour it found."""
    priceList = [
        _row(0, 0.30, 0.30, load=5000, localTime="2026-01-01 21:00"),
        _row(1, 0.30, 0.30, load=5000, localTime="2026-01-01 22:00"),
        _row(2, 0.02, 0.02, load=5000, localTime="2026-01-01 23:00"),   # cheap
        _row(3, 0.25, 0.25, load=5000, localTime="2026-01-01 20:00"),   # LAST: window end
    ]
    reserveWh = planner.calcTerminalReserveWh(
        priceList=priceList, ratedBatteryCapacity=27900, reserveFloorPct=15,
        reserveMarginPct=25, **COMMON)
    # 2 hours (21, 22) x 5000 Wh load x 1.25 margin, floor (4185 Wh) well below this
    assert reserveWh == 12500


def test_reserve_never_exceeds_battery_capacity(planner):
    priceList = [
        _row(0, 0.30, 0.30, load=50000, localTime="2026-01-01 21:00"),
        _row(1, 0.30, 0.30, load=50000, localTime="2026-01-01 22:00"),
        _row(2, 0.02, 0.02, load=50000, localTime="2026-01-01 23:00"),
        _row(3, 0.25, 0.25, load=50000, localTime="2026-01-01 20:00"),
    ]
    reserveWh = planner.calcTerminalReserveWh(
        priceList=priceList, ratedBatteryCapacity=27900, reserveFloorPct=15,
        reserveMarginPct=25, **COMMON)
    assert reserveWh == 27900


def test_disabled_reserve_returns_zero(planner):
    priceList = [_row(0, 0.30, 0.30, load=5000, localTime="2026-01-01 20:00")]
    reserveWh = planner.calcTerminalReserveWh(
        priceList=priceList, useTerminalReserve=False, ratedBatteryCapacity=27900,
        reserveFloorPct=15, **COMMON)
    assert reserveWh == 0


def test_empty_price_list_returns_zero(planner):
    reserveWh = planner.calcTerminalReserveWh(
        priceList=[], ratedBatteryCapacity=27900, reserveFloorPct=15, **COMMON)
    assert reserveWh == 0
