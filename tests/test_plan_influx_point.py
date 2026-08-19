"""writePlanToInflux() - the fields the Battery Plan dashboard reads off the `plan` point.

Same seam as test_app_settings_influx.py, one measurement over: the dashboard queries these
fields by name, so a rename here goes unnoticed until a panel is blank on the NAS.

price_market is the one worth pinning. Until 2026-08-03 the price line came from the
collector's own `market_price` series instead, refreshed every three hours from a feed that
publishes tomorrow later than the auction the planner reads - so the panel drew bars and
thresholds across a tomorrow with no price under them, which looks exactly like a quiet
market. Storing the planner's own price is what lets the dashboard draw the prices the plan
was actually optimised against.
"""
from datetime import datetime, timedelta, timezone


T0 = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
MARKET = [0.0895, 0.2210]


def _fields(line):
    return dict(kv.split("=", 1) for kv in line.split(" ")[1].split(","))


def _plan_lines(planner, monkeypatch):
    return [line for line in _write(planner, monkeypatch) if line.startswith("plan,")]


def _write(planner, monkeypatch):
    """Run writePlanToInflux() over a two-interval plan, capturing what it would write.

    influx_source is patched through monkeypatch rather than assigned: it is a real
    imported module, shared with every other test in the session, and the planner fixture
    re-executes Marstek-planning.py - which calls influx_source.configured() at import - for
    each one. A stub left behind breaks the next test's import, not this test.
    """
    written = []
    monkeypatch.setattr(planner, "writePlansToInflux", True)
    monkeypatch.setattr(planner, "influxAvailable", True)
    monkeypatch.setattr(planner.influx_source, "writePoints",
                        lambda lines, **kw: written.extend(lines) or len(lines))
    monkeypatch.setattr(planner.influx_source, "config",
                        lambda: {"plan_bucket": "planning"})
    planner.pvForecastRawWh = {}
    planner.priceList = [
        # seq, market EUR/kWh, utc, local, pvDirect, pvIndirect, load, buy, sell
        [n, MARKET[n],
         (T0 + timedelta(minutes=15 * n)).strftime("%Y-%m-%d %H:%M"),
         "local", 0, 100, 300, MARKET[n] * 1.21 + 0.15, MARKET[n]]
        for n in range(len(MARKET))
    ]
    schedule = [{"soc": 9000, "charge": 900, "discharge": 0, "import": 800, "export": 0,
                 "costs": 0.07, "reserve": 5000},
                {"soc": 8100, "charge": 0, "discharge": 900, "import": 0, "export": 800,
                 "costs": -0.19, "reserve": 5000}]
    planner.writePlanToInflux(schedule, "2026-08-03T12:00:00Z")
    return written


def test_the_fields_the_dashboard_queries_are_all_present(planner, monkeypatch):
    fields = _fields(_plan_lines(planner, monkeypatch)[0])
    assert set(fields) == {
        "soc_wh", "capacity_wh", "charge_wh", "discharge_wh", "import_wh", "export_wh",
        "cost_eur", "reserve_wh", "price_buy", "price_sell", "price_market",
        "pv_forecast_wh", "pv_forecast_raw_wh", "load_forecast_wh"}


def test_every_point_carries_the_capacity_soc_is_a_fraction_of(planner, monkeypatch):
    """alphaess-collector's dashboards divide soc_wh by their OWN copy of 27900.

    hardware.py fixed that drift inside this repo (CODE-REVIEW.md D4) but cannot reach
    across the boundary, because no test in either repo crosses it -- so a capacity change
    applied on one side and not the other renders a plausible, wrong percentage rather than
    an error. Sending the number with the data it explains is what removes the copies.
    """
    lines = _plan_lines(planner, monkeypatch)
    assert lines
    for line in lines:
        assert float(_fields(line)["capacity_wh"]) == float(planner.ratedBatteryCapacity)


def test_it_is_the_capacity_this_plan_was_optimised_against(planner, monkeypatch):
    """`ratedBatteryCapacity`, NOT `hardware.CAPACITY_WH`.

    BT_CAP and the Domoticz user variable both override it, so the default is not
    necessarily what a given plan was planned against -- on a backtest it is wrong every
    time. Publishing the default would move the same mismatch one layer down and hide it
    better, so this is pinned rather than left to the reader of the line above.
    """
    monkeypatch.setattr(planner, "ratedBatteryCapacity", 12345)
    for line in _plan_lines(planner, monkeypatch):
        assert float(_fields(line)["capacity_wh"]) == 12345.0


def test_the_default_run_publishes_the_shared_constant(planner, monkeypatch):
    """The ordinary case, tying this back to hardware.py: with nothing overridden, what
    reaches InfluxDB is the same number tests/test_hardware.py guards."""
    import hardware
    line = _plan_lines(planner, monkeypatch)[0]
    assert float(_fields(line)["capacity_wh"]) == float(hardware.CAPACITY_WH)


def test_capacity_is_a_field_not_a_tag(planner, monkeypatch):
    """A tag would start a fresh series on every capacity change, and tags are indexed
    strings -- the consumers need a number to divide by."""
    line = _plan_lines(planner, monkeypatch)[0]
    tags = dict(kv.split("=", 1) for kv in line.split(" ")[0].split(",")[1:])
    assert set(tags) == {"plan_run"}


def test_price_market_is_the_raw_market_price_not_the_all_in_one(planner, monkeypatch):
    """The dashboard's price line and the app's High/Low bands are both set against the
    market signal. Storing only price_buy would not do: tax, VAT and saldering cannot be
    backed out per interval."""
    lines = _plan_lines(planner, monkeypatch)
    assert [float(_fields(line)["price_market"]) for line in lines] == MARKET
    assert float(_fields(lines[0])["price_buy"]) != MARKET[0]


def test_the_line_and_the_thresholds_drawn_over_it_come_from_one_price(planner, monkeypatch):
    """The panel draws price_market and the dashed app_setting thresholds on one axis.
    Both must be built from the same number, or a trade appears on the wrong side of its
    own threshold and arithmetic that is correct looks broken."""
    written = _write(planner, monkeypatch)
    plan = [line for line in written if line.startswith("plan,")]
    settings = [line for line in written if line.startswith("app_setting,")]
    assert settings, "expected the trading plan to produce band recommendations"

    prices = [float(_fields(line)["price_market"]) for line in plan]
    for line in settings:
        threshold = float(_fields(line)["set_to_eur_kwh"])
        assert min(prices) <= threshold <= max(prices), (line, prices)
