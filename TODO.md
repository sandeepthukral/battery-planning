# What is left to do

State at the end of 2026-07-30. The planner runs unattended on the NAS hourly (it was every
3 hours when this was written; the move to hourly came with the PV-cache widening), the
day-after report runs at 06:10, and both feed Grafana. What remains is listed here rather
than in `NAS-DEPLOYMENT-PLAN.md`, which is the record of *how the thing was built* and is
long enough already.

Three kinds of item, kept apart because they need different things: **work** needs someone
to do it, **waiting** needs time to pass, **deferred** needs a decision that has already
been made to not decide yet.

---

## First thing tomorrow

**1. Verify the annotated-CSV fix in the container.** One command, read-only, no `--write`
so it cannot disturb the stored score:

```sh
ssh -t data42 'cd /volume1/docker/battery-planning && sudo /usr/local/bin/docker compose run --rm --no-deps planner python3 /app/report_day.py 2026-07-30'
```

Expect ~23 scored intervals and no traceback. 30 July is the right day to test with: its
plans straddle the addition of `pv_forecast_raw_wh`, so the query returns runs with 11
fields and runs with 12, which is exactly the mixed-schema case that used to crash with
`ValueError: Invalid isoformat string: '_time'`. The image was rebuilt with the fix on
2026-07-30 but never exercised.

**2. Read the 06:10 report.** `data/reports/report_20260731.txt` on the NAS, and the
**AlphaESS Plan vs Actual** dashboard. This is the first genuinely complete day:

- no "intervals had no plan in force" note, because plans now exist from before midnight
- a whole-day money figure, so the partial-window caveat about SoC crossing the boundary
  no longer applies
- the first PV forecast error measured against a full day rather than one evening

Everything before this was a partial evening and the numbers flattered the battery.

---

## Work

Ordered by size, not by importance. The first two are small and real; start there.

### Swap the local `.env` admin token for the scoped one

`INFLUX_TOKEN` in the Mac's `.env` is the InfluxDB **admin** token: write access to every
bucket, including the collector's `alphaess`. The NAS already uses a scoped
`INFLUX_TOKEN_PLANNING`, and `influx_source.config()` already prefers it. Only the laptop
still holds the wide one, and the laptop is the machine most likely to be running an
experiment against the live database.

Create a token scoped `r alphaess, rw planning`, put it in `.env` as
`INFLUX_TOKEN_PLANNING`, and delete `INFLUX_TOKEN`. Verify with `python3 influx_source.py`.

### `influxProfileDays=7` returns 8 days

The load profile asks for 7 days of history and gets 8. An off-by-one in the range, almost
certainly a `>=` where a `>` belongs, or the range being built from a date rather than an
instant. Harmless in effect -- an extra day of load barely moves a 7-day mean -- but it
means the profile is not what the configuration says it is, and the next person to tune
`influxProfileDays` will be tuning something else.

### Eyeball the Grafana **plan** dashboard

The score dashboard was checked query by query against the live database. Its
forward-looking sibling never was, beyond confirming panels render. Worth one look at:

- price drawn as a line on the right-hand axis in ct/kWh, not as a filled area on the kWh axis
- charge/discharge bars legible at the ~1.2 kWh scale they actually occupy
- the reserve floor dashed and distinguishable from planned SoC
- legend reading `charge` / `discharge` / `buy price` / `sell price` rather than four `Value`s

That last one is the known failure mode: a series that leaves Flux in `_value` arrives in
Grafana as the literal "Value", every `byName` override misses, and the panel silently falls
back to its defaults. Fixed once already; worth confirming it stayed fixed.

### Weekday/weekend split in the load profile

The profile averages the last 7 days with no regard for which day of the week they were, so
Sunday's shape is mixed into Tuesday's forecast. There is no longer any historical load data
to widen this with -- the immediate past is the only forecast available -- so the fix is to
weight or bucket by day type rather than to fetch more.

**Do not start this before reading a week of reports.** The load forecast error is now
measured daily, on the dashboard and in the text report. If it is small, this is not worth
the complexity; if it is systematically worse at weekends, the dashboard will say so and the
fix is obvious. Measuring first costs a week and nothing else.

`fit_load_profile.py`'s DAY TYPE table is the instrument for this. As of 2026-08-02 it has
about three days of plan history, so it is not readable yet; it needs roughly three weeks.

### A temperature term in the load forecast

Last on the list of forecast improvements, behind day-type bucketing, sub-hourly shape and a
longer profile window, and it is the only one needing a data source the house does not have.
Do not start it until those three are exhausted -- and note that in a Dutch summer this house
may show no temperature dependence at all, which would say nothing about January.

The data is being collected in the meantime, by `capture_weather.py` on the planner's schedule
and `backfill_weather.py` for history, because a forecast that was never stored cannot be
scored later. Nothing reads it.

### Weather lives in the `planning` bucket, which is the wrong bucket

`weather_observed` and `weather_forecast` are actuals and forecasts about the outside world,
not plan output, and they belong beside load and PV in `alphaess`. They are in `planning`
because the planner's token is read:alphaess + write:planning, so it is the only bucket this
repo can write to at all.

The cost is retention: `planning` expires at 400 days, `alphaess` never does. So a fit that
one day wants five winters of temperature against load will find the early ones gone.

Accepted rather than fixed, because observations can be re-backfilled from a free API at any
time — `backfill_weather.py --from <date>` — so the loss is recoverable with one command. The
forecast series is not recoverable, and that is the part to watch: if it is still wanted in
2027, either move the writer into alphaess-collector or widen the token.

### `BT_ETAX` is a single global, but energy tax is per calendar year

0.12286 in 2025, 0.11085 in 2026. `plan-now.sh` picks by year and warns for 2027, so the
**live path is correct**. The planner itself takes one number for a whole run, so a backtest
spanning 1 January prices both sides at one year's rate.

Same class of silent wrongness that saldering had before `salderingApplies(localDate)` --
and the fix has the same shape: decide per interval from the interval's own date, not once
per run.

### Phase 6 -- impute the five zero-load days

`clean_backtest_csv.py` drops 2025-07-09, 07-28, 08-07, 09-05 and 09-06 outright, whole days
of `load_kwh == 0`. Impute from the same weekday in surrounding weeks instead, and keep them
flagged as imputed in the sidecar. All five predate the P1 window (starts 2026-01-22), so
P1 cannot recover them and imputation is the only option.

---

## Waiting on time, not on effort

### Phase 5 -- PV calibration

`fit_pv_elevation.py` is built and its fit path is verified against synthetic data. It
currently reports, correctly, that there is nothing to fit. `pv_forecast_raw_wh` has been
stored since the 23:05 run on 2026-07-30, so the clock started then.

| | when |
|---|---|
| Level (`pvOverallCalibration`, still an unfitted 1.00) | ~3-5 weeks |
| Shape above 35 degrees | same window |
| Shape below 20 degrees -- the steep end, 0.20 to 0.57 | **December-January** |

The steep end cannot be fitted from summer data at any sample size. At 52.5N the sun peaks
near 14 degrees in late December, so a whole winter day sits in those bands; in summer they
occur only at dawn and dusk with the sun in the north-east and north-west, over different
obstructions than the winter south. Only 4% of summer energy is made below 20 degrees.

Run `python3 fit_pv_elevation.py 30` occasionally. It says how thin each band is and refuses
to conclude rather than fitting anyway.

---

## Deferred by decision

- **Phase 7** -- imbalance/intraday pricing, post-2027 network tariffs, netcongestie.
  Recorded, not built.
- **KNMI HARMONIE-AROME** as a PV forecast source. Researched: real integration target, but
  each model run is a ~1.4 GB tar needing GRIB2 decoding, which is a large step up from
  forecast.solar's single JSON call. Not until the forecast error justifies it -- which is
  now measured, so that is a question with an answer coming.
- **Battery capacity 27,900 -> ~30,500 Wh.** Parked: "Let's not change the capacity of the
  battery right now."
- **`soc_curve.py` -- fitted, committed, and deliberately not wired in.** A measured
  non-linear Wh-per-SoC-point curve (see the module docstring for the derivation and its
  4.3% vs 11.5% MAE against the flat constant), rebuildable with
  `scripts/fit_soc_curve.py`. Nothing imports it: the planner still plans on
  `hardware.CAPACITY_WH`'s flat 279 Wh/point. Note that `soc_curve.CAPACITY_WH` is the
  integral of the fitted curve and does *not* agree with `hardware.CAPACITY_WH` -- that is
  the same question as the capacity item above, arrived at from the other end, and the two
  should be settled together. Wiring it in means touching the optimiser's SoC accounting,
  which is a separate piece of work with its own backtest.
  `scripts/fit_soc_curve.py` needs numpy and scipy, which `requirements.txt` does not carry
  (the fit is a laptop job, not something the NAS image runs).

---

## Closed, so nobody re-opens them

- Retention on the `planning` bucket -- **400 days**, already set. The `plan_run` tag adds
  ~2,900 series a year and this is what stops it growing forever.
- Measured power limits 4700 W discharge / 4850 W charge -- already in `run-matrix.sh`.
- The launchd agent on the Mac -- unloaded and the plist deleted. The NAS is the only thing
  that plans.
- `/volume1/docker/battery-archive` -- deleted, it was the incomplete copy. Three good
  copies remain: the Mac, `documents/` on the NAS, and the `-nopii` tarball on Google Drive.
