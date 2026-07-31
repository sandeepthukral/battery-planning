#!/usr/bin/env python3
"""Repair the APsystems EMA hourly export before it is used for backtesting.

The raw download carries two upstream defects (see README / audit):

  1. Catch-up spikes. When the EMA logger drops out it writes the accumulated
     energy into the first hour it comes back, producing single hours above the
     array nameplate (e.g. 2025-09-04 18:00 = 8.61 kWh from a 4.98 kWp array,
     immediately before a 48h outage). Day totals stay plausible; only the
     intra-day distribution is wrong. These are redistributed across the
     under-reported hours of the same day, preserving the day total exactly.

  2. Missing load written as zero. Whole days of load_kwh == 0.0000 while solar
     keeps recording. A house never draws zero for 24h. These days are dropped
     and listed in the sidecar exclusion file rather than imputed - inventing a
     load profile would be fabricating data.

Daylight-saving transition days are reported but NOT repaired: the raw export
has a flat 24 rows/day, so the merged/dropped hour is unrecoverable.

The raw CSV is never modified. Usage:

    python3 clean_backtest_csv.py [raw.csv] [clean.csv]
"""
import csv
import json
import math
import os
import sys
import collections
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import solar

RAW = sys.argv[1] if len(sys.argv) > 1 else "backtest_input_hourly.csv"
CLEAN = sys.argv[2] if len(sys.argv) > 2 else "backtest_input_hourly_clean.csv"
EXCLUDED = CLEAN.rsplit(".", 1)[0] + ".excluded.json"

NAMEPLATE_KWP = 4.98            # 12 x 415 Wp, see pvGroups in Marstek-planning.py
LAT, LON = 52.5, 5.5            # Flevoland
TZ = ZoneInfo("Europe/Amsterdam")
UTC = ZoneInfo("UTC")

d2r, r2d = math.radians, math.degrees


def clearsky_weight(dt_local):
    """Relative clear-sky yield weight for an hour; 0 when the sun is down.

    Elevation at the MIDPOINT of the hour starting at dt_local, hence the +30min -
    the same convention Marstek-planning.py's own solarElevation() uses (see
    solar.py, CODE-REVIEW.md D5, which now holds the one copy of the formula itself).
    """
    elev = solar.elevation(LAT, LON, dt_local + timedelta(minutes=30))
    if elev <= 0:
        return 0.0
    z = d2r(90 - elev)
    return max(0.0, 1098 * math.cos(z) * math.exp(-0.059 / math.cos(z)))


def redistribute(day_rows, cap):
    """Move catch-up-spike energy back onto the hours it was taken from.

    Preserves the day total exactly. Returns (new_values, moved_kwh, n_spikes).
    """
    vals = [v for _, v in day_rows]
    total = sum(vals)
    weights = [clearsky_weight(dt) for dt, _ in day_rows]
    wsum = sum(weights)
    if wsum <= 0 or total <= 0:
        return vals, 0.0, 0
    target = [total * w / wsum for w in weights]

    new = list(vals)
    spikes = [i for i, v in enumerate(vals) if v > cap]
    if not spikes:
        return vals, 0.0, 0

    pool = 0.0
    for i in spikes:
        keep = min(vals[i], max(target[i], 0.0))
        pool += vals[i] - keep
        new[i] = keep

    # push the pool onto hours reporting less than their clear-sky share,
    # iterating so that no hour is pushed above the nameplate cap
    for _ in range(6):
        if pool <= 1e-9:
            break
        deficit = [max(0.0, target[i] - new[i]) if new[i] < cap else 0.0
                   for i in range(len(new))]
        dsum = sum(deficit)
        if dsum <= 1e-9:
            room = [max(0.0, cap - new[i]) if weights[i] > 0 else 0.0
                    for i in range(len(new))]
            rsum = sum(room)
            if rsum <= 1e-9:
                break
            for i in range(len(new)):
                new[i] += pool * room[i] / rsum
            pool = 0.0
            break
        give = min(pool, dsum)
        for i in range(len(new)):
            new[i] += give * deficit[i] / dsum
        pool -= give

    return new, sum(vals[i] - new[i] for i in spikes), len(spikes)


def main():
    with open(RAW) as f:
        rows = list(csv.DictReader(f))
    print(f"read {len(rows)} rows from {RAW}")

    parsed = []
    for r in rows:
        dt = datetime.strptime(r["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
        parsed.append((dt, float(r["load_kwh"]), float(r["solar_kwh"]), r["datetime"]))

    by_day = collections.OrderedDict()
    for dt, load, solar, raw_ts in parsed:
        by_day.setdefault(raw_ts[:10], []).append((dt, load, solar, raw_ts))

    # --- defect 2: whole days of zero load -----------------------------------
    zero_load_days = [d for d, rs in by_day.items() if sum(r[1] for r in rs) == 0]

    # --- DST reporting only ---------------------------------------------------
    dst_days = []
    for d, rs in by_day.items():
        day = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=TZ)
        nxt = (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)).replace(tzinfo=TZ)
        # subtracting two aware datetimes that share a tzinfo is wall-clock arithmetic
        # and always yields 24h; convert to UTC so the DST offset is actually applied
        expected = round((nxt.astimezone(UTC) - day.astimezone(UTC)).total_seconds() / 3600)
        if expected != len(rs):
            dst_days.append((d, len(rs), expected))

    # --- defect 1: catch-up spikes -------------------------------------------
    cap = NAMEPLATE_KWP
    repaired = {}
    total_moved = 0.0
    spike_days = []
    for d, rs in by_day.items():
        if max(r[2] for r in rs) <= cap:
            continue
        new, moved, n = redistribute([(r[0], r[2]) for r in rs], cap)
        repaired[d] = new
        total_moved += moved
        spike_days.append((d, n, moved, sum(r[2] for r in rs)))

    # --- write -----------------------------------------------------------------
    kept = 0
    with open(CLEAN, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["datetime", "load_kwh", "solar_kwh"])
        for d, rs in by_day.items():
            if d in zero_load_days:
                continue
            fixed = repaired.get(d)
            for i, (dt, load, solar, raw_ts) in enumerate(rs):
                s = fixed[i] if fixed is not None else solar
                w.writerow([raw_ts, f"{load:.4f}", f"{s:.4f}"])
                kept += 1

    with open(EXCLUDED, "w") as f:
        json.dump({"excluded_dates": zero_load_days,
                   "reason": "load_kwh == 0 for the whole day (missing data written as zero)"},
                  f, indent=2)

    # --- report ----------------------------------------------------------------
    print(f"\ncatch-up spikes repaired on {len(spike_days)} days "
          f"({total_moved:.1f} kWh moved, day totals preserved):")
    for d, n, moved, tot in spike_days:
        print(f"    {d}  {n} spike hour(s)  {moved:5.2f} kWh redistributed  (day total {tot:.2f} kWh unchanged)")

    print(f"\ndropped {len(zero_load_days)} zero-load days -> {EXCLUDED}:")
    for d in zero_load_days:
        print(f"    {d}")

    print(f"\nDST days left unrepaired (flat 24 rows in the raw export, hour unrecoverable):")
    for d, got, exp in dst_days:
        print(f"    {d}  has {got} rows, local day is {exp}h")

    print(f"\nwrote {kept} rows to {CLEAN}")


if __name__ == "__main__":
    main()
