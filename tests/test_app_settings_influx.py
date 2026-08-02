"""appSettingLines() - the seam between app_bands and the stored points the dashboard reads.

test_app_bands.py proves the arithmetic. This proves the arithmetic actually reaches
InfluxDB in a shape the dashboard can use: right measurement, right tags, and the fields
it queries by name. A rename on either side of this seam is invisible until a panel goes
blank on the NAS, which is not where anyone wants to find it.
"""
from datetime import datetime, timedelta, timezone


T0 = datetime(2026, 8, 2, 18, 0, tzinfo=timezone.utc)


def _rows(spec, minutes=15):
    rows = []
    for n, (ct, act) in enumerate(spec):
        row = {"ts": T0 + timedelta(minutes=minutes * n), "price": ct / 100.0,
               "charge": 0, "discharge": 0, "import": 0, "export": 0, "soc": 9000}
        if act == "sell":
            row["discharge"], row["export"] = 900, 800
        elif act == "buy":
            row["charge"], row["import"] = 900, 800
        rows.append(row)
    return rows


def _fields(line):
    body = line.split(" ")[1]
    return dict(kv.split("=", 1) for kv in body.split(","))


def _tags(line):
    return dict(kv.split("=", 1) for kv in line.split(" ")[0].split(",")[1:])


def test_no_plan_rows_writes_nothing(planner):
    assert planner.appSettingLines([], "2026-08-02T18:00:00Z") == []


def test_a_plan_with_no_trading_writes_nothing(planner):
    assert planner.appSettingLines(_rows([(10, None)] * 4), "r") == []


def test_one_point_per_session_tagged_with_its_direction(planner):
    lines = planner.appSettingLines(
        _rows([(20, "sell"), (20, "sell"), (10, None), (2, "buy")]), "r1")
    assert len(lines) == 2
    assert all(line.startswith("app_setting,") for line in lines)
    assert sorted(_tags(line)["action"] for line in lines) == ["buy", "sell"]
    assert all(_tags(line)["plan_run"] == "r1" for line in lines)


def test_the_fields_the_dashboard_queries_are_all_present(planner):
    line = planner.appSettingLines(_rows([(20, "sell"), (19, "sell")]), "r")[0]
    assert set(_fields(line)) == {
        "set_to_eur_kwh", "until_s", "target_soc_wh", "exact", "extra_intervals",
        "intervals", "energy_wh"}


def test_the_point_is_timestamped_at_the_start_of_the_session(planner):
    """Not at the plan run, and not at the end - the dashboard's time axis puts this row
    where the setting has to be entered."""
    lines = planner.appSettingLines(_rows([(10, None), (20, "sell"), (19, "sell")]), "r")
    assert lines[0].rsplit(" ", 1)[1] == str(int((T0 + timedelta(minutes=15)).timestamp()))


def test_until_is_stored_as_seconds_the_dashboard_can_rebuild(planner):
    line = planner.appSettingLines(_rows([(20, "sell"), (19, "sell")]), "r")[0]
    assert float(_fields(line)["until_s"]) == (T0 + timedelta(minutes=30)).timestamp()


def test_exactness_is_stored_as_a_number_because_influx_fields_are_floats(planner):
    """influx_source.linePoint casts every field to float, so a bool would arrive as 0.0/1.0
    anyway. Storing the int makes that explicit rather than incidental."""
    spike = _rows([(20, "sell"), (19, "sell")] + [(10, None)] * 4 + [(25, None)])
    line = planner.appSettingLines(spike, "r")[0]
    assert float(_fields(line)["exact"]) == 0.0
    assert float(_fields(line)["extra_intervals"]) == 1.0
