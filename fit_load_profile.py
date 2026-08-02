#!/usr/bin/env python3
"""Measure the house load forecast against what the house actually used.

    python3 fit_load_profile.py [days]        # default 30

The load forecast is the last `influxProfileDays` complete days of measured load, averaged
per hour of day and weighted towards the recent ones (`influx_source.hourlyAvgProfileWh`,
called from `calcHourlyAvgUsage`). That is the only load forecast that exists. It has no
calibration layer, so unlike the PV forecast there is no knob here to fit -- this reports
where the profile is biased, and the fix for a bias is a change to how the profile is built.

Three questions, in the order they should be asked:

    LEVEL     total measured against total forecast. A level error means the 7-day window is
              lagging a trend, not that a constant is mistuned.
    SHAPE     the same ratio per hour of day. This is where a profile that is right in total
              and wrong all day shows up, and it is what the overnight reserve depends on.
    DAY TYPE  the same ratio split weekday against weekend. The profile averages the last
              seven days without regard for which day of the week they were, so Sunday's
              shape is mixed into Tuesday's forecast. TODO.md names the split as the likely
              fix and says not to start it before reading a week of reports; this is what
              reading them looks like.

An interval is compared against the plan in force for it -- the most recent run at or before
it, the same rule report_day.py uses -- so the forecast being judged is the freshest one that
existed, and this measures the profile's bias rather than how forecasts decay with lead time.

Unlike the PV equivalent, intervals with a zero forecast are kept. A zero-forecast interval
with real load in it is not a night-time non-event to be filtered out; it is the error worth
seeing.
"""
import argparse
import math
import os
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import influx_source as ix
import report_day as rd

# Below this the per-interval ratio stops meaning anything: a 3 Wh forecast against 30 Wh
# measured is a 10x error about nothing. The floor only filters the +/- column, never the
# energy-weighted ratio, which is immune to the problem because it sums first.
RATIO_FLOOR_WH = 10.0

# A cell thinner than this is reported but not to be concluded from. Weekend cells fill at
# 2/7 the rate of weekday ones, so this bites the day-type table first and by design.
THIN = 20


def collect(days):
    stop = datetime.now(ix.LOCAL_TZ)
    start = stop - timedelta(days=days)
    points = ix.planPoints(start, stop, "plan")
    if not points:
        return None
    chosen, _, runs = rd.inForcePlans(points)

    times = sorted(chosen)
    minutes = rd.intervalMinutes(set(times))
    measured = ix.intervalEnergyWh(ix.FIELD_LOAD, start, stop, minutes, clamp_negative=True)

    rows = []
    # Counted separately so that "the field is not being written" and "no measured interval
    # has come back yet" do not look the same. Without this the first real failure is
    # indistinguishable from waiting.
    withField = 0
    for t in times:
        fc = chosen[t].get("load_forecast_wh")
        act = measured.get(t)
        if fc is not None:
            withField += 1
        if fc is None or act is None:
            continue
        rows.append((t, float(fc), float(act)))
    return {"rows": rows, "runs": runs, "start": start, "stop": stop,
            "minutes": minutes, "planned": len(times), "withField": withField}


def stderr(values):
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return math.sqrt(var / n)


def isWeekend(when):
    return when.weekday() >= 5


def byHour(rows):
    """rows -> {hour: [(forecast, actual), ...]}, keyed by local hour of day."""
    out = {}
    for when, fc, act in rows:
        out.setdefault(when.hour, []).append((fc, act))
    return out


def summarise(pairs):
    """(n, forecastWh, measuredWh, ratio, stderrOfRatio) for one bucket.

    ratio is energy-weighted - sum(measured)/sum(forecast) - because that is the quantity a
    change to the profile would have to move. The standard error is over per-interval ratios
    instead, which answers a different question: how consistent the bias is, rather than how
    big. They are deliberately not the same statistic.
    """
    fcSum = sum(p[0] for p in pairs)
    actSum = sum(p[1] for p in pairs)
    ratio = actSum / fcSum if fcSum > 0 else float("nan")
    se = stderr([a / f for f, a in pairs if f >= RATIO_FLOOR_WH])
    return len(pairs), fcSum, actSum, ratio, se


def _ratioRow(label, pairs):
    n, fcSum, actSum, ratio, se = summarise(pairs)
    return "  %-9s %6d %9.2f  %9.2f  %6.3f  %s%s" % (
        label, n, fcSum / 1000.0, actSum / 1000.0, ratio,
        ("%5.3f" % se) if se is not None else "    -",
        "   thin" if n < THIN else "")


def _parseDays(value):
    try:
        days = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a whole number of days, got %r" % value)
    if days < 1:
        raise argparse.ArgumentTypeError("must be at least 1 day, got %d" % days)
    return days


def _parseArgs(argv):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("days", nargs="?", type=_parseDays, default=30, metavar="DAYS",
                        help="how many days back to read (default: 30)")
    return parser.parse_args(argv)


def main(argv):
    ns = _parseArgs(argv)
    days = ns.days
    if not ix.configured():
        print("InfluxDB is not configured; run 'python3 influx_source.py' to see what is missing.")
        return 2

    d = collect(days)
    if d is None:
        print("No plans stored in the last %d days at all." % days)
        return 1
    if not d["rows"]:
        print("Nothing to fit yet, over the last %d days." % days)
        print("  past intervals with a plan in force  : %d" % d["planned"])
        print("  of those, carrying load_forecast_wh  : %d" % d["withField"])
        if d["withField"] == 0:
            print("\nThe planner is not writing load_forecast_wh. Either the plans were made")
            print("with includeUsage off, or calcHourlyAvgUsage found no load history and")
            print("planned with zero expected load - which it warns about at plan time.")
        else:
            print("\nThe forecast is there but no interval has a measured value beside it.")
            print("That is the collector, not the planner: check that power_readings is")
            print("still being written, with 'python3 influx_source.py'.")
        return 1

    rows = d["rows"]
    totalFc = sum(r[1] for r in rows)
    totalAct = sum(r[2] for r in rows)

    print("Load forecast against measured, %s -> %s"
          % (d["start"].strftime("%Y-%m-%d"), d["stop"].strftime("%Y-%m-%d")))
    print("%d intervals of %d minutes, from %d plan runs\n"
          % (len(rows), d["minutes"], d["runs"]))

    # --- LEVEL -------------------------------------------------------------------------
    print("LEVEL")
    print("  forecast         %8.1f kWh" % (totalFc / 1000.0))
    print("  measured         %8.1f kWh" % (totalAct / 1000.0))
    if totalFc > 0:
        print("  measured/forecast   %6.3f" % (totalAct / totalFc))
    print("  NOTE: there is no pvOverallCalibration equivalent to move here. The forecast is")
    print("        the house's own recent load played back, so a level error means the")
    print("        profile window is lagging a trend rather than that a constant is wrong.")

    # --- SHAPE -------------------------------------------------------------------------
    hours = byHour(rows)
    print("\nSHAPE")
    print("  hour           n   forecast   measured   ratio   +/-")
    print("  " + "-" * 55)
    for h in sorted(hours):
        print(_ratioRow("%02d:00" % h, hours[h]))
    print("\n  'ratio' is measured/forecast, energy-weighted within the hour. Above 1.00 the")
    print("  house used more than the plan expected. The evening rows are the ones that cost")
    print("  money: calcTerminalReserveWh() sizes the overnight reserve from the forecast")
    print("  load in those hours and adds a flat 25% margin, so an evening ratio steadily")
    print("  above 1.25 means the reserve is undersized on a typical night, not a rare one.")

    # --- DAY TYPE ----------------------------------------------------------------------
    weekday = [r for r in rows if not isWeekend(r[0])]
    weekend = [r for r in rows if isWeekend(r[0])]
    print("\nDAY TYPE")
    print("  bucket         n   forecast   measured   ratio   +/-")
    print("  " + "-" * 55)
    print(_ratioRow("weekday", [(f, a) for _, f, a in weekday]) if weekday
          else "  weekday        0         -          -       -       -")
    print(_ratioRow("weekend", [(f, a) for _, f, a in weekend]) if weekend
          else "  weekend        0         -          -       -       -")

    if weekday and weekend:
        print("\n  by hour        weekday ratio   weekend ratio   n wd / n we")
        print("  " + "-" * 58)
        wdHours, weHours = byHour(weekday), byHour(weekend)
        for h in sorted(set(wdHours) | set(weHours)):
            wd = wdHours.get(h, [])
            we = weHours.get(h, [])
            print("  %02d:00          %13s   %13s   %5d / %d"
                  % (h,
                     ("%6.3f" % summarise(wd)[3]) if wd else "-",
                     ("%6.3f" % summarise(we)[3]) if we else "-",
                     len(wd), len(we)))

    print("\n  This is the table TODO.md's weekday/weekend item waits on. If the two ratio")
    print("  columns track each other, splitting the profile by day type buys nothing and the")
    print("  complexity is not worth it. If they diverge, and diverge in the same hours on")
    print("  most days rather than on one memorable Sunday, the fix is a bucketed profile in")
    print("  influx_source.hourlyAvgProfileWh().")

    # --- is there enough of it yet -----------------------------------------------------
    print()
    thin = [h for h in sorted(hours) if len(hours[h]) < THIN]
    if thin and len(thin) == len(hours):
        # Naming all 24 hours is not a diagnosis, it is the whole table again. On a short run
        # that is the only case, and it deserves the shorter sentence.
        # Per day each hour gains 60/minutes intervals, so the wait is in days only after
        # dividing by that. At the 15-minute MTU the two differ by a factor of four.
        perDay = max(1, int(round(60.0 / d["minutes"])))
        wait = int(math.ceil((THIN - min(len(v) for v in hours.values())) / float(perDay)))
        print("TOO THIN to conclude anywhere: every hour has under %d intervals. Nothing"
              % THIN)
        print("below the LEVEL block is worth reading yet - come back in about %d day%s."
              % (max(1, wait), "" if max(1, wait) == 1 else "s"))
    elif thin:
        print("TOO THIN to conclude in these hours (under %d intervals): %s"
              % (THIN, ", ".join("%02d:00" % h for h in thin)))
    if weekend:
        print("Weekend intervals fill at 2/7 the rate of weekday ones, so the weekend column is")
        print("always the thinner of the two and always the later one to trust: over %d days it"
              % days)
        print("carries %d intervals against the weekday %d." % (len(weekend), len(weekday)))
    print("None of this separates a bad profile from a genuinely unusual fortnight. A single")
    print("hot week, a holiday, or one appliance failing will move these ratios exactly like a")
    print("systematic bias does, and only a longer run tells them apart.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
