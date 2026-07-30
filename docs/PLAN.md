# The plan file

`Marstek-planning.py` writes one plan table per run. This document describes that file: where
it comes from, what each column means, and how it is consumed downstream.

## Where it comes from

`Marstek-planning.py` itself writes to `entsoe-output<startdate>.txt` in the working directory
(`outputOptimisationResult()`, `Marstek-planning.py:1799`). `plan-now.sh` runs the planner and
then renames that file to `plans/plan_<YYYYMMDD>_<HH>.txt`, where the date/hour are the run's
start date and start hour (`plan-now.sh:70-71`):

```
plan=plans/plan_${today}_${hour}.txt
mv entsoe-output${today}.txt $plan
```

So `plans/plan_20260730_09.txt` is the plan produced by the run started on 2026-07-30 at 09:00
local time. A matching log lives at `logs/plan_20260730_09.log`.

`plan-now.sh` then feeds the file to `advise.py`, which turns it into a human-readable block
summary (see below).

## File format

One line per interval (hourly, or every 15 minutes for quarter-hour planning — the default
since NL day-ahead moved to 15-minute MTU on 2025-10-01). A header line is printed once, at the
top of the file:

```
date        time   pvD   pvI   use  nett chrgD  chrg dschg   soc   imp   exp  pr-buy pr-sell    cost
```

Example rows (`plans/plan_20260730_09.txt`):

```
2026-07-30 09:00     0   106   153    47     0     0     0  1897    47     0 +0.298534 +0.298534 -0.014000
2026-07-30 09:15     0   106   153    47     0   940     0  2790   987     0 +0.278533 +0.278533 -0.274900
```

If the solver did not reach an optimal solution, an `ATTENTION` line is written above the table
instead of (or in addition to) rows for that day:

```
ATTENTION: no optimal solution achieved, status is <status> on date <runDate>
```

### Columns

| Column    | Unit    | Meaning |
|-----------|---------|---------|
| `date`    | —       | Local calendar date of the interval start |
| `time`    | —       | Local time of the interval start (`HH:MM`) |
| `pvD`     | Wh      | Forecast (or actual) PV production from "direct" panel groups — panels DC/MPPT-coupled straight into the battery. This household has none (all panels are AC-coupled/"indirect"), so `pvD` is always `0` here; see the group comment near the top of `Marstek-planning.py`. |
| `pvI`     | Wh      | Forecast (or actual) PV production from "indirect" panel groups — AC-coupled into the house consumer unit, competing with usage/export/charging in the grid balance |
| `use`     | Wh      | Forecast (or actual) household consumption for the interval |
| `nett`    | Wh      | `use - pvI - pvD`: the interval's net position before any battery/grid action. Positive = shortfall, negative = surplus. |
| `chrgD`   | Wh      | Same value as `pvD` — the "direct" PV feed charging the battery. Kept as its own column because it lines up with `chrg`/`dschg` in the charge/discharge block of the row. |
| `chrg`    | Wh      | Battery charge decided by the optimiser, from grid import and/or indirect PV surplus |
| `dschg`   | Wh      | Battery discharge decided by the optimiser |
| `soc`     | Wh      | Battery state of charge at the end of the interval |
| `imp`     | Wh      | Grid import for the interval |
| `exp`     | Wh      | Grid export for the interval |
| `pr-buy`  | €/kWh   | Buy price for the interval (incl. VAT/energy tax/network/supplier costs when tax is included) |
| `pr-sell` | €/kWh   | Sell/return price for the interval (accounts for the netting/saldering regime in effect on that date when `-n` is used) |
| `cost`    | €       | Net cost of the interval: `pr-sell/1000 * exp - pr-buy/1000 * imp`. Negative = money spent, positive = money earned. |

Integer columns (`pvD` … `exp`) are printed right-aligned in 5-character fields. `pr-buy` and
`pr-sell` are signed with 6 decimal places; `cost` is signed with 6 decimal places and a wider
integer part. See `printIntervalToFile()` (`Marstek-planning.py:1833`) for the exact format
strings.

### Which days get written

* The header line is only printed for the first day of the run (`runDate==startDateObject`).
* If the run's end date is exactly one day after the start date, every interval on the list is
  written (this is the live-run / `plan-now.sh` case, which passes tomorrow as `BT_END`).
* Otherwise (multi-day / backtest runs), each day's rows are truncated at 15:00 local time —
  the boundary at which the next day's run would take over — so consecutive daily plans chain
  together without overlapping or gaping.

## How the file is consumed

`advise.py` (`python3 advise.py plan_20260727_13.txt`) reads the table, groups consecutive
intervals with the same action (`BUY` grid-charge, `SOLAR` charge from PV, `SELL` discharge to
grid, `COVER` discharge to house, `SPILL` export surplus PV, `GRID` run the house off the grid,
`IDLE`), and prints one block per action with its energy, average price, and euro value. It
parses the file by column position, matching the `COLS` list in `advise.py:24` to the header
above — if the column set in `printIntervalToFile()` ever changes, `advise.py`'s `COLS` list
must change with it.

With `--actuals` (and `INFLUX_HOST` set), `advise.py` also reads what the battery actually did
over the plan's window from InfluxDB, so a plan can be checked against reality.

`plan-now.sh` also runs `advise.py --min-hours 12` as a sanity check: if the plan's horizon is
shorter than 12 hours (e.g. a stale price cache before tomorrow's day-ahead auction published),
the run is treated as failed.
