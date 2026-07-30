#!/usr/bin/env python3
"""Export measured load and PV from InfluxDB into the backtest CSV format.

The year-long backtest CSV comes from the AlphaESS/APsystems portal downloads. For
recent days the alphaess-collector InfluxDB is a better source: 30s samples rather
than portal hourly buckets, so none of the catch-up-spike misattribution that
clean_backtest_csv.py has to repair.

Writes the same three columns Marstek-planning.py expects:

    datetime,load_kwh,solar_kwh      (datetime is Europe/Amsterdam, hour start)

Usage:

    INFLUX_HOST=192.168.68.105 python3 influx_to_backtest_csv.py 2026-07-26 2026-07-28 out.csv

The end date is EXCLUSIVE. Give it one day beyond the last day you want to plan, so
the planner's ~48h horizon has data to work with.
"""
import csv
import sys
from datetime import datetime, timedelta

import influx_source as ix


def export(start_date, end_date, path):
    start = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=ix.LOCAL_TZ)
    stop = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=ix.LOCAL_TZ)

    load = ix.hourlyEnergyWh(ix.FIELD_LOAD, start, stop, clamp_negative=True)
    pv = ix.hourlyEnergyWh(ix.FIELD_PV, start, stop, clamp_negative=True)

    hours = []
    t = start
    while t < stop:
        hours.append(t)
        t += timedelta(hours=1)

    written = missing = 0
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["datetime", "load_kwh", "solar_kwh"])
        for t in hours:
            key = t.strftime("%Y-%m-%d %H")
            if key not in load and key not in pv:
                # no usable samples: leave the row out entirely rather than write a
                # zero, which the optimiser would read as a genuinely idle hour
                missing += 1
                continue
            w.writerow([t.strftime("%Y-%m-%d %H:00:00"),
                        "%.4f" % (load.get(key, 0.0) / 1000.0),
                        "%.4f" % (pv.get(key, 0.0) / 1000.0)])
            written += 1

    print("wrote %s: %d hours (%d skipped for missing/low coverage)" % (path, written, missing))
    print("  load  total %8.2f kWh" % (sum(load.values()) / 1000.0))
    print("  solar total %8.2f kWh" % (sum(pv.values()) / 1000.0))
    by_day = {}
    for key, val in load.items():
        by_day.setdefault(key[:10], [0.0, 0.0])[0] += val / 1000.0
    for key, val in pv.items():
        by_day.setdefault(key[:10], [0.0, 0.0])[1] += val / 1000.0
    print("\n  date          load_kWh  solar_kWh  hours")
    counts = {}
    for key in set(list(load) + list(pv)):
        counts[key[:10]] = counts.get(key[:10], 0) + 1
    for day in sorted(by_day):
        print("  %s   %8.2f   %8.2f   %3d" % (day, by_day[day][0], by_day[day][1], counts.get(day, 0)))
    return written


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(0 if export(sys.argv[1], sys.argv[2], sys.argv[3]) else 1)
