"""mergeForecastWithPricelist / mergeUsageWithPriceList / mergeActualWithPricelist,
which read/write the module-global priceList directly (CODE-REVIEW.md D3: these three
used to each re-implement their own linear-scan lookup; now they share
_buildLookupTable() and mergeUsageWithPriceList/mergeActualWithPricelist share one
_mergeLoadIntoPriceList() body).
"""


def _row(seq, hour, day="2026-01-01"):
    # matches Marstek-planning.py's priceList row shape
    return [seq, 0.20, "%s %02d:00" % (day, hour), "%s %02d:00" % (day, hour),
            0, 0, 0, 0.20, 0.20]


# --- _buildLookupTable() --------------------------------------------------------------


def test_lookup_table_single_key_field(planner):
    rows = [["00", 100], ["01", 200], ["23", 999]]
    table = planner._buildLookupTable(rows, (0,), 1)
    assert table == {("00",): 100, ("01",): 200, ("23",): 999}


def test_lookup_table_two_key_fields(planner):
    rows = [[1, "2026-01-01", "00", 500], [2, "2026-01-02", "00", 600]]
    table = planner._buildLookupTable(rows, (1, 2), 3)
    assert table[("2026-01-01", "00")] == 500
    assert table[("2026-01-02", "00")] == 600


def test_lookup_table_empty_rows(planner):
    assert planner._buildLookupTable([], (0,), 1) == {}


# --- mergeUsageWithPriceList() / mergeActualWithPricelist() ---------------------------


def test_merge_usage_matches_by_hour_only_ignoring_date(planner, monkeypatch):
    """usageList rows are ["HH", Wh] - one profile that applies to every day, matched
    on hour alone (this is the forecast-load path: today's 14:00 uses the same
    profile entry as tomorrow's 14:00)."""
    monkeypatch.setattr(planner, "outputMode", False)
    planner.hourAvgPlanning = True
    planner.priceList = [_row(0, 14, day="2026-01-01"), _row(1, 14, day="2026-01-02")]
    planner.mergeUsageWithPriceList([["14", 750]])
    assert planner.priceList[0][planner.IDX_LOAD] == 750
    assert planner.priceList[1][planner.IDX_LOAD] == 750


def test_merge_usage_missing_hour_adds_nothing(planner, monkeypatch):
    monkeypatch.setattr(planner, "outputMode", False)
    planner.hourAvgPlanning = True
    planner.priceList = [_row(0, 9)]
    planner.mergeUsageWithPriceList([["14", 750]])   # no entry for hour 09
    assert planner.priceList[0][planner.IDX_LOAD] == 0


def test_merge_usage_adds_to_existing_load_rather_than_overwriting(planner, monkeypatch):
    monkeypatch.setattr(planner, "outputMode", False)
    planner.hourAvgPlanning = True
    row = _row(0, 14)
    row[planner.IDX_LOAD] = 100   # something already merged in (e.g. by a prior call)
    planner.priceList = [row]
    planner.mergeUsageWithPriceList([["14", 750]])
    assert planner.priceList[0][planner.IDX_LOAD] == 850


def test_merge_actual_matches_by_date_and_hour(planner, monkeypatch):
    """actualList rows are [seq, date, hour, Wh] - one instant each, matched on BOTH
    date and hour (the historical-actuals path: 2026-01-01 14:00 must not pick up
    2026-01-02 14:00's value)."""
    monkeypatch.setattr(planner, "outputMode", False)
    planner.hourAvgPlanning = True
    planner.priceList = [_row(0, 14, day="2026-01-01"), _row(1, 14, day="2026-01-02")]
    planner.mergeActualWithPricelist([[1, "2026-01-01", "14", 500],
                                       [2, "2026-01-02", "14", 600]])
    assert planner.priceList[0][planner.IDX_LOAD] == 500
    assert planner.priceList[1][planner.IDX_LOAD] == 600


def test_merge_usage_and_actual_divide_by_intervals_per_hour(planner, monkeypatch):
    """Quarter-hour mode: a whole-hour Wh figure must be divided by 4 before being
    added to a 15-minute interval - CODE-REVIEW.md D2's intervalsPerHour(), exercised
    here through the D3 merge."""
    monkeypatch.setattr(planner, "outputMode", False)
    planner.hourAvgPlanning = False   # quarter-hour mode -> intervalsPerHour() == 4
    planner.priceList = [_row(0, 14)]
    planner.mergeUsageWithPriceList([["14", 800]])
    assert planner.priceList[0][planner.IDX_LOAD] == 200


# --- mergeForecastWithPricelist() ------------------------------------------------------


def test_merge_forecast_indirect_group_adds_to_pv_indirect(planner, monkeypatch):
    monkeypatch.setattr(planner, "outputMode", False)
    monkeypatch.setattr(planner, "pvCalibrateForecast", False)
    planner.hourAvgPlanning = True
    planner.priceList = [_row(0, 10)]
    planner.pvForecastRawWh = {}
    forecastList = [[0, "2026-01-01", "10", 900]]
    planner.mergeForecastWithPricelist(["indirect", 10, 0, 1.245], forecastList, applyCalibration=False)
    assert planner.priceList[0][planner.IDX_PV_INDIRECT] == 900
    assert planner.priceList[0][planner.IDX_PV_DIRECT] == 0


def test_merge_forecast_direct_group_adds_to_pv_direct(planner, monkeypatch):
    monkeypatch.setattr(planner, "outputMode", False)
    monkeypatch.setattr(planner, "pvCalibrateForecast", False)
    planner.hourAvgPlanning = True
    planner.priceList = [_row(0, 10)]
    planner.pvForecastRawWh = {}
    forecastList = [[0, "2026-01-01", "10", 900]]
    planner.mergeForecastWithPricelist(["direct", 10, 0, 1.245], forecastList, applyCalibration=False)
    assert planner.priceList[0][planner.IDX_PV_DIRECT] == 900


def test_merge_forecast_records_raw_wh_keyed_by_utc_time(planner, monkeypatch):
    """pvForecastRawWh is keyed by the interval's UTC start string (IDX_TIME_UTC), not
    position - dropHistoryFromPricelist()/dropUnpublishedFromPricelist() run after this
    and would otherwise make an index quietly mean a different interval."""
    monkeypatch.setattr(planner, "outputMode", False)
    monkeypatch.setattr(planner, "pvCalibrateForecast", False)
    planner.hourAvgPlanning = True
    planner.priceList = [_row(0, 10)]
    planner.pvForecastRawWh = {}
    forecastList = [[0, "2026-01-01", "10", 900]]
    planner.mergeForecastWithPricelist(["indirect", 10, 0, 1.245], forecastList, applyCalibration=False)
    utcKey = planner.priceList[0][planner.IDX_TIME_UTC]
    assert planner.pvForecastRawWh[utcKey] == 900


def test_merge_forecast_missing_hour_adds_nothing(planner, monkeypatch):
    monkeypatch.setattr(planner, "outputMode", False)
    monkeypatch.setattr(planner, "pvCalibrateForecast", False)
    planner.hourAvgPlanning = True
    planner.priceList = [_row(0, 3)]   # no matching forecast row for hour 03
    planner.pvForecastRawWh = {}
    planner.mergeForecastWithPricelist(["indirect", 10, 0, 1.245], [[0, "2026-01-01", "10", 900]],
                                        applyCalibration=False)
    assert planner.priceList[0][planner.IDX_PV_INDIRECT] == 0
