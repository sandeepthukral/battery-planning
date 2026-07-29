#!/usr/bin/env python3
"""Build a backtest CSV from Sparky P1 smart-meter data.

The P1 meter records what crossed the utility connection, not what the house used.
Before the battery was installed there is nothing else on the connection, so the
house load follows from an energy balance:

    load = solar + delivery - return

which is the identity already cross-validated against the APsystems EMA export:
P1-derived load matches EMA load to within 1.6% in the worst month and under 0.6%
for March-June 2026.

That identity holds ONLY while no battery is present. Once the battery is charging
and discharging, the same meter reading is consistent with many different loads:

    load = solar + delivery - return + discharge - charge

and P1 alone cannot separate the terms. Hence --until, defaulting to the day before
the battery went in. Data past that date is not "slightly worse", it is wrong, and
the default exists so nobody has to remember why.

This gives two things the EMA export cannot:

  1. A second, independent measurement of 2026-01-22 -> 2026-06-30, so that stretch
     is no longer single-copy. The EMA export cannot be re-downloaded.
  2. Coverage of 2026-07-01 -> 07-16, the gap between the EMA export ending and the
     alphaess-collector InfluxDB history starting on 2026-07-17.

For (2) a solar source is still needed, since P1 sees only the net at the meter.
Pass it with --solar; the flag is repeatable and later files win, so the EMA export
supplies Jan-Jun and a July-only file fills the tail:

    python3 p1_to_backtest_csv.py backtest_p1.csv \\
        --solar backtest_input_hourly.csv --solar solar_july_2026.csv

A solar file needs a "datetime" column (Europe/Amsterdam, hour start) and a
"solar_kwh" column. The EMA export already matches; anything else can be trimmed to
those two columns.

Nothing is imputed. An hour missing any of its four P1 intervals, or missing solar,
is dropped and listed in the sidecar exclusion file next to the output.
"""
import argparse
import collections
import csv
import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Amsterdam")

# The Sparky export lives outside the repo on purpose: it carries the meter EAN, the
# meter number and the service address, and this repo is a public fork. See
# NAS-DEPLOYMENT-PLAN.md, "Irreplaceable data".
DEFAULT_P1 = os.environ.get(
    "P1_CSV",
    "/Users/sandeep/Personal/battery-data/sparky-export-20260724/p1_elec_15min_agg.csv")

# Last day on which the meter still saw the house alone. The battery was commissioned
# after this, and from then on P1 cannot be turned into load - see the module docstring.
DEFAULT_UNTIL = "2026-07-16"

INTERVALS_PER_HOUR = 4           # the export is 15-minute
HOURS_PER_INTERVAL = 0.25        # avg power in kW x 0.25 h = kWh


def readP1(path, until_date, from_date=None):
    """Sum the 15-minute average powers into local hourly kWh of delivery and return.

    Returns (hourly, partial) where hourly maps a local hour to (delivery_kwh,
    return_kwh) and partial lists the hours that did not have all four intervals.
    Timestamps in the export are UTC; everything downstream of here is local time,
    because that is what the planner and the EMA export both use.
    """
    deliv = collections.defaultdict(float)
    ret = collections.defaultdict(float)
    seen = collections.Counter()

    with open(path) as f:
        for r in csv.DictReader(f):
            t = datetime.fromisoformat(r["time"].replace(" ", "T")).astimezone(TZ)
            day = t.strftime("%Y-%m-%d")
            if day > until_date:
                continue
            if from_date and day < from_date:
                continue
            h = t.replace(minute=0, second=0, microsecond=0)
            deliv[h] += float(r["avg_delivery_power"]) * HOURS_PER_INTERVAL
            ret[h] += float(r["avg_return_power"]) * HOURS_PER_INTERVAL
            seen[h] += 1

    hourly, partial = {}, []
    for h in sorted(seen):
        # A short hour understates both flows and would show up as a plausible-looking
        # low-load hour rather than as missing data. Drop it rather than scale it up:
        # scaling assumes the missing quarter looked like the rest of the hour, which is
        # exactly the assumption that fails around switch-on and switch-off events.
        if seen[h] != INTERVALS_PER_HOUR:
            partial.append((h, seen[h]))
            continue
        hourly[h] = (deliv[h], ret[h])
    return hourly, partial


def readSolar(paths):
    """Merge one or more solar sources into local-hour -> kWh. Later files win."""
    solar = {}
    for p in paths:
        n = 0
        with open(p) as f:
            reader = csv.DictReader(f)
            if "solar_kwh" not in reader.fieldnames:
                raise SystemExit("ERROR: %s has no solar_kwh column (found: %s)"
                                 % (p, ",".join(reader.fieldnames or [])))
            for r in reader:
                dt = datetime.strptime(r["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
                solar[dt] = float(r["solar_kwh"])
                n += 1
        print("solar: %5d hours from %s" % (n, p))
    return solar


def compareWithEMA(rows, ema_path, negative):
    """Month-by-month check of P1-derived load against the EMA export, where they overlap.

    This is the reason to trust the output at all, so it runs by default rather than
    living in a separate script nobody remembers to run.

    Read the diff column together with the negative-hour figure printed underneath it.
    The two are the same phenomenon. EMA is known to misplace solar in time without
    losing it, so its hourly solar runs low in some hours and high in others; the low
    ones surface here as impossible negative loads and get dropped, while the high ones
    stay and inflate the total. Dropping one tail of a symmetric error biases the sum
    upward by roughly the energy in the dropped hours - and that is exactly what is
    observed, month by month. With nothing excluded the two sources agree to under 1%.
    """
    if not os.path.exists(ema_path):
        return
    ema = {}
    with open(ema_path) as f:
        for r in csv.DictReader(f):
            dt = datetime.strptime(r["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
            ema[dt] = float(r["load_kwh"])

    mon = collections.defaultdict(lambda: [0.0, 0.0, 0])
    for dt, load, _ in rows:
        if dt not in ema:
            continue
        m = dt.strftime("%Y-%m")
        mon[m][0] += load
        mon[m][1] += ema[dt]
        mon[m][2] += 1
    if not mon:
        print("\nno overlap with %s - nothing to cross-check" % ema_path)
        return

    negmon = collections.defaultdict(float)
    for h, v in negative:
        negmon[h.strftime("%Y-%m")] += v

    print("\ncross-check against %s (overlapping hours only):" % ema_path)
    print("    %-8s %9s %9s %8s %7s %11s %9s"
          % ("month", "P1 load", "EMA load", "diff", "hours", "dropped -", "adj diff"))
    for m in sorted(mon):
        a, b, n = mon[m]
        # Adding the dropped negative energy back undoes the one-sided exclusion, so
        # "adj diff" answers the question that actually matters: do the two sources
        # agree on how much energy the house used? The unadjusted "diff" is inflated
        # by however much was dropped.
        adj = a + negmon.get(m, 0.0)
        print("    %-8s %9.1f %9.1f %7.2f%% %7d %11.1f %8.2f%%"
              % (m, a, b, 100 * (a - b) / b if b else 0, n,
                 negmon.get(m, 0.0), 100 * (adj - b) / b if b else 0))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", nargs="?", default="backtest_p1_hourly.csv")
    ap.add_argument("--p1", default=DEFAULT_P1, help="Sparky p1_elec_15min_agg.csv")
    ap.add_argument("--solar", action="append", default=[],
                    help="solar source; repeatable, later files win")
    ap.add_argument("--until", default=DEFAULT_UNTIL,
                    help="last day to include, inclusive (default %s: the battery went "
                         "in after this and P1 can no longer give load)" % DEFAULT_UNTIL)
    ap.add_argument("--from", dest="from_date", default=None, help="first day to include")
    ap.add_argument("--ema", default="backtest_input_hourly.csv",
                    help="EMA export to cross-check against; skipped if absent")
    args = ap.parse_args()

    if not args.solar:
        args.solar = ["backtest_input_hourly.csv"]

    hourly, partial = readP1(args.p1, args.until, args.from_date)
    print("P1   : %5d complete hours from %s" % (len(hourly), args.p1))
    solar = readSolar(args.solar)

    rows, no_solar, negative = [], [], []
    for h in sorted(hourly):
        if h not in solar:
            no_solar.append(h)
            continue
        deliv, ret = hourly[h]
        load = solar[h] + deliv - ret
        if load < 0:
            # Physically impossible without a battery: the house cannot export more than
            # it generates. Means the solar figure is too low for this hour, so the hour
            # is unusable rather than merely noisy.
            negative.append((h, load))
            continue
        rows.append((h, load, solar[h]))

    with open(args.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["datetime", "load_kwh", "solar_kwh"])
        for dt, load, sol in rows:
            w.writerow([dt.strftime("%Y-%m-%d %H:%M:%S"), "%.4f" % load, "%.4f" % sol])

    excluded = args.out.rsplit(".", 1)[0] + ".excluded.json"
    with open(excluded, "w") as f:
        json.dump({
            "source": args.p1,
            "solar_sources": args.solar,
            "until": args.until,
            "until_reason": "battery commissioned after this date; load = solar + delivery"
                            " - return only holds while nothing else is on the connection",
            "partial_hours": [{"hour": h.strftime("%Y-%m-%d %H:%M:%S"),
                               "intervals": n} for h, n in partial],
            "hours_without_solar": [h.strftime("%Y-%m-%d %H:%M:%S") for h in no_solar],
            "hours_negative_load": [{"hour": h.strftime("%Y-%m-%d %H:%M:%S"),
                                     "load_kwh": round(v, 4)} for h, v in negative],
        }, f, indent=2)

    compareWithEMA(rows, args.ema, negative)

    print("\nexcluded: %d partial hours, %d without a solar figure, %d negative load -> %s"
          % (len(partial), len(no_solar), len(negative), excluded))
    if no_solar:
        first, last = no_solar[0], no_solar[-1]
        print("    missing solar spans %s -> %s; supply it with --solar to recover these"
              % (first.strftime("%Y-%m-%d"), last.strftime("%Y-%m-%d")))
    print("wrote %d rows to %s (%s -> %s)"
          % (len(rows), args.out,
             rows[0][0].strftime("%Y-%m-%d") if rows else "-",
             rows[-1][0].strftime("%Y-%m-%d") if rows else "-"))


if __name__ == "__main__":
    main()
