"""app_bands.appSettings - the thresholds to type into the alphaess app.

The claim under test is narrow and strong: the emitted threshold makes the app trade in
exactly the intervals the plan trades in, no more and no fewer. Most tests here therefore
do not assert a number. They build a scenario, take the recommendation, and then *simulate
the app* against it - `_appWould()` below - and demand the result equal the plan. A test
that pinned the number instead would keep passing while the recommendation drifted away
from the thing the number is for.

Prices are EUR/kWh throughout, market basis. The fixtures read in ct via `_p()` because
that is how they are read on the dashboard and typed into the phone.
"""
from datetime import datetime, timedelta, timezone

import pytest

import app_bands
from app_bands import BUY, SELL, appSettings

T0 = datetime(2026, 8, 2, 18, 0, tzinfo=timezone.utc)


def _p(ct):
    return round(ct / 100.0, 6)


def _rows(spec, socWh=14000, startAt=T0, minutes=15):
    """Build plan intervals from a compact spec of (price_ct, action) pairs.

    action is "sell", "buy", or one of the near-misses that must NOT be classified as
    trading: "solar" (battery charges from the roof, no grid import), "cover" (battery
    runs the house, nothing exported), "spill" (PV surplus exported, battery idle),
    "grid" (house imports, battery idle), or None for idle.
    """
    rows = []
    for n, (ct, act) in enumerate(spec):
        row = {"ts": startAt + timedelta(minutes=minutes * n), "price": _p(ct),
               "charge": 0, "discharge": 0, "import": 0, "export": 0, "soc": socWh}
        if act == "sell":
            row["discharge"], row["export"] = 900, 800
        elif act == "buy":
            row["charge"], row["import"] = 900, 800
        elif act == "solar":
            row["charge"] = 900
        elif act == "cover":
            row["discharge"] = 900
        elif act == "spill":
            row["export"] = 800
        elif act == "grid":
            row["import"] = 800
        rows.append(row)
    return rows


def _appWould(rows, setting):
    """Indices the alphaess app would trade in, given one setting, while it is live.

    This is the app's whole behaviour: a bare threshold comparison against the market
    price, applied continuously from the moment the setting is entered until the next one
    replaces it. The comparison is strict in both directions.
    """
    live = [i for i, r in enumerate(rows) if r["ts"] >= setting["start"]]
    if setting.get("liveUntil") is not None:
        live = [i for i in live if rows[i]["ts"] < setting["liveUntil"]]
    if setting["action"] == SELL:
        return [i for i in live if rows[i]["price"] > setting["setTo"]]
    return [i for i in live if rows[i]["price"] < setting["setTo"]]


def _planned(rows, action, setting):
    """Indices the plan trades in, over the same window the setting is live."""
    live = [i for i, r in enumerate(rows) if r["ts"] >= setting["start"]]
    if setting.get("liveUntil") is not None:
        live = [i for i in live if rows[i]["ts"] < setting["liveUntil"]]
    return [i for i in live if app_bands.classifyInterval(rows[i]) == action]


def _withLiveWindows(settings):
    """Annotate each setting with when the next one of the same direction replaces it."""
    for action in (SELL, BUY):
        same = [s for s in settings if s["action"] == action]
        for n, s in enumerate(same):
            s["liveUntil"] = same[n + 1]["start"] if n + 1 < len(same) else None
    return settings


def _assertReproducesPlan(rows, settings):
    """Every exact setting must make the app do precisely what the plan does."""
    _withLiveWindows(settings)
    for s in settings:
        if not s["exact"]:
            continue
        assert _appWould(rows, s) == _planned(rows, s["action"], s), (
            "setting %r does not reproduce the plan" % (s,))


# --- classification: the near misses ----------------------------------------------------


@pytest.mark.parametrize("act", ["solar", "cover", "spill", "grid", None])
def test_only_grid_trades_count_as_trading(act):
    """Charging off the roof is not buying, and running the house off the battery is not
    selling. Both would otherwise drag a threshold to a price the plan never traded at."""
    assert appSettings(_rows([(10, act)] * 4)) == []


def test_charge_with_import_is_a_buy_and_charge_without_is_not():
    mixed = _rows([(5, "buy"), (5, "solar")])
    assert [app_bands.classifyInterval(r) for r in mixed] == [BUY, None]


def test_discharge_with_export_is_a_sell_and_discharge_without_is_not():
    mixed = _rows([(20, "sell"), (20, "cover")])
    assert [app_bands.classifyInterval(r) for r in mixed] == [SELL, None]


# --- session merging --------------------------------------------------------------------


def test_gap_of_three_quarters_merges_into_one_session():
    rows = _rows([(20, "sell"), (20, "sell"),
                  (10, None), (10, None), (10, None),
                  (20, "sell"), (20, "sell")])
    settings = appSettings(rows)
    assert len(settings) == 1
    assert settings[0]["intervals"] == 4
    _assertReproducesPlan(rows, settings)


def test_gap_of_four_quarters_stays_two_sessions():
    """The tolerance is a boundary, so it needs a test on each side of it."""
    rows = _rows([(20, "sell"), (20, "sell"),
                  (10, None), (10, None), (10, None), (10, None),
                  (20, "sell"), (20, "sell")])
    assert len(appSettings(rows)) == 2


def test_gap_containing_the_opposite_trade_never_merges():
    """Sell, buy, sell inside four quarters is not one selling session - the buy is a real
    boundary, and merging across it would hand one threshold a window it cannot serve."""
    rows = _rows([(20, "sell"), (2, "buy"), (20, "sell")])
    sells = [s for s in appSettings(rows) if s["action"] == SELL]
    assert len(sells) == 2


def test_a_single_interval_is_a_session():
    rows = _rows([(10, None), (20, "sell"), (10, None)])
    settings = appSettings(rows)
    assert len(settings) == 1 and settings[0]["intervals"] == 1
    _assertReproducesPlan(rows, settings)


def test_sell_and_buy_sessions_are_independent_and_time_ordered():
    rows = _rows([(20, "sell"), (20, "sell"), (10, None), (2, "buy"), (2, "buy"),
                  (10, None), (21, "sell")])
    settings = appSettings(rows)
    assert [s["action"] for s in settings] == [SELL, BUY, SELL]
    assert settings == sorted(settings, key=lambda s: s["start"])


def test_no_trading_at_all_yields_nothing():
    assert appSettings(_rows([(10, None)] * 8)) == []


def test_empty_plan_yields_nothing():
    assert appSettings([]) == []


# --- the threshold reproduces the plan --------------------------------------------------


def test_sell_threshold_sits_between_the_quiet_high_and_the_trading_low():
    rows = _rows([(18, "sell"), (17, "sell"), (12, None), (11, None)])
    setting = appSettings(rows)[0]
    assert setting["exact"]
    assert 0.12 <= setting["setTo"] < 0.17
    _assertReproducesPlan(rows, [setting])


def test_buy_threshold_sits_between_the_trading_high_and_the_quiet_low():
    rows = _rows([(3, "buy"), (5, "buy"), (14, None), (15, None)])
    setting = appSettings(rows)[0]
    assert setting["exact"]
    assert 0.05 < setting["setTo"] <= 0.14
    _assertReproducesPlan(rows, [setting])


def test_gap_inside_a_merged_session_must_not_trade():
    """The quarter the plan sits out is the whole reason the check is two-sided: a
    threshold at the session's marginal price would sell into it."""
    rows = _rows([(20, "sell"), (20, "sell"), (13, None), (19, "sell"), (19, "sell")])
    setting = appSettings(rows)[0]
    assert setting["exact"]
    assert 0.13 <= setting["setTo"] < 0.19
    _assertReproducesPlan(rows, [setting])


def test_sell_threshold_stays_above_a_quiet_price_just_under_the_session():
    """The bound that is easiest to drop silently. Rounding a tenth of a cent below the
    cheapest planned sale looks right and passes every test where the quiet prices are far
    away - it only trades wrongly when one of them sits just underneath."""
    rows = _rows([(17.5, "sell"), (17.0, "sell"), (16.98, None)])
    setting = appSettings(rows)[0]
    assert setting["exact"]
    assert 0.1698 <= setting["setTo"] < 0.17
    _assertReproducesPlan(rows, [setting])


def test_buy_threshold_stays_below_a_quiet_price_just_above_the_session():
    rows = _rows([(3.0, "buy"), (5.0, "buy"), (5.02, None)])
    setting = appSettings(rows)[0]
    assert setting["exact"]
    assert 0.05 < setting["setTo"] <= 0.0502
    _assertReproducesPlan(rows, [setting])


def test_a_spike_after_the_session_breaks_exactness():
    """The setting entered at 18:00 is still live at 03:00. A threshold that ignores what
    happens between sessions would call this exact while the battery sold into the spike."""
    rows = _rows([(20, "sell"), (19, "sell")] + [(10, None)] * 4 + [(25, None)])
    setting = appSettings(rows)[0]
    assert not setting["exact"]
    assert setting["extra"] == 1


def test_a_spike_after_the_session_is_fine_once_the_next_session_takes_over():
    """Same spike, but now the plan sells into it - so it belongs to the next session's
    window, and the first setting is exact after all."""
    rows = _rows([(20, "sell"), (19, "sell")] + [(10, None)] * 4 + [(25, "sell")])
    settings = appSettings(rows)
    assert len(settings) == 2
    assert all(s["exact"] for s in settings)
    _assertReproducesPlan(rows, settings)


def test_infeasible_sell_still_catches_every_planned_sale():
    """When the quiet high is above the trading low no threshold works. The recommendation
    then errs towards trading too much rather than too little: an extra sale happens at a
    price the plan was itself willing to sell at, while a missed sale leaves energy in a
    battery the plan needed empty."""
    rows = _rows([(16, "sell"), (20, "sell"), (18, None)])
    setting = appSettings(rows)[0]
    assert not setting["exact"]
    assert setting["extra"] == 1
    _withLiveWindows([setting])
    assert set(_planned(rows, SELL, setting)) <= set(_appWould(rows, setting))


def test_infeasible_buy_still_catches_every_planned_purchase():
    rows = _rows([(8, "buy"), (2, "buy"), (5, None)])
    setting = appSettings(rows)[0]
    assert not setting["exact"]
    assert setting["extra"] == 1
    _withLiveWindows([setting])
    assert set(_planned(rows, BUY, setting)) <= set(_appWould(rows, setting))


def test_no_quiet_intervals_leaves_only_the_one_sided_constraint():
    rows = _rows([(20, "sell"), (18, "sell")])
    setting = appSettings(rows)[0]
    assert setting["exact"] and setting["extra"] == 0
    assert setting["setTo"] < 0.18


# --- boundaries and awkward prices ------------------------------------------------------


def test_quiet_price_equal_to_the_trading_low_is_not_exact():
    """A tie admits nothing: the app compares strictly, so any T below the trading price
    is also below the identical quiet one."""
    rows = _rows([(17, "sell"), (17, None)])
    assert not appSettings(rows)[0]["exact"]


def test_window_too_narrow_for_the_tenth_of_a_cent_grid_falls_back_finer():
    """0.1 ct is what gets typed, but a real threshold at an awkward value beats a round
    one that trades wrongly."""
    rows = _rows([(17.06, "sell"), (17.02, None)])
    setting = appSettings(rows)[0]
    assert setting["exact"]
    assert 0.1702 <= setting["setTo"] < 0.1706
    _assertReproducesPlan(rows, [setting])


def test_negative_prices_are_handled():
    """2026-08-03 planned a charge at -0.002 ct. Nothing here may assume prices are
    positive, least of all the rounding."""
    rows = _rows([(-2, "buy"), (0.5, "buy"), (9, None)])
    setting = appSettings(rows)[0]
    assert setting["exact"]
    assert 0.005 < setting["setTo"] <= 0.09
    _assertReproducesPlan(rows, [setting])


def test_hourly_plans_work_as_well_as_quarter_hourly():
    rows = _rows([(20, "sell"), (19, "sell"), (10, None)], minutes=60)
    setting = appSettings(rows)[0]
    assert setting["until"] - setting["start"] == timedelta(hours=2)
    _assertReproducesPlan(rows, [setting])


# --- what the table shows ---------------------------------------------------------------


def test_until_is_the_end_of_the_last_trading_interval_not_its_start():
    rows = _rows([(20, "sell"), (19, "sell"), (10, None)])
    setting = appSettings(rows)[0]
    assert setting["start"] == T0
    assert setting["until"] == T0 + timedelta(minutes=30)


def test_target_soc_is_the_planned_soc_at_the_end_of_the_session():
    rows = _rows([(20, "sell"), (19, "sell"), (10, None)])
    rows[0]["soc"], rows[1]["soc"], rows[2]["soc"] = 14000, 9000, 9000
    assert appSettings(rows)[0]["targetSocWh"] == 9000


def test_target_soc_ignores_intervals_after_the_session_ends():
    """The number is what the battery should read when the setting is replaced, so a later
    solar charge must not move it."""
    rows = _rows([(20, "sell"), (10, "solar")])
    rows[0]["soc"], rows[1]["soc"] = 9000, 12000
    assert appSettings(rows)[0]["targetSocWh"] == 9000


def test_energy_is_summed_over_the_session_only():
    rows = _rows([(20, "sell"), (13, None), (19, "sell")])
    assert appSettings(rows)[0]["energyWh"] == 1800


# --- a realistic day --------------------------------------------------------------------


def test_the_2026_08_03_shape_clusters_seven_bands_into_three_sessions():
    """The day that prompted this: seven contiguous selling bands, four of them a single
    quarter-hour, which a human reads as three sessions. Prices approximate the published
    day-ahead; the point is the shape, not the decimals."""
    spec = (
        [(17.4, "sell")] * 10 + [(16.5, "sell")] * 2      # evening block
        + [(16.4, None)]                                   # one quarter out
        + [(16.2, "sell")] * 4
        + [(15.0, None)] * 20                              # overnight, well below
        + [(16.5, "sell")] * 4 + [(16.4, None)] + [(16.7, "sell")]   # morning block
        + [(13.0, None)] * 4
        + [(8.9, "buy")] * 4 + [(3.1, "buy")] * 8 + [(7.1, "buy")] * 8   # midday charge
        + [(12.0, None)] * 4
        + [(17.2, "sell")] + [(16.9, None)] * 2 + [(16.8, "sell")] * 8   # next evening
    )
    settings = appSettings(_rows(spec))
    assert [s["action"] for s in settings] == [SELL, SELL, BUY, SELL]
    _assertReproducesPlan(_rows(spec), settings)


def test_the_midday_buy_session_is_one_unbroken_band():
    rows = _rows([(13.0, None)] * 4 + [(8.9, "buy")] * 4 + [(3.1, "buy")] * 8
                 + [(7.1, "buy")] * 8 + [(12.0, None)] * 4)
    buys = [s for s in appSettings(rows) if s["action"] == BUY]
    assert len(buys) == 1 and buys[0]["intervals"] == 20
    assert buys[0]["exact"]
    _assertReproducesPlan(rows, buys)
