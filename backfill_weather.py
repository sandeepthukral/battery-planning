"""Backfill measured hourly weather into the plan bucket, as measurement weather_observed.

    python3 backfill_weather.py [days]                 # default 30, back from today
    python3 backfill_weather.py --from 2026-07-17      # to today
    python3 backfill_weather.py --from 2026-07-17 --to 2026-07-31
    python3 backfill_weather.py 30 --dry-run           # print the points, write nothing
    python3 backfill_weather.py --from 2026-07-17 --via forecast

--via picks which endpoint the history comes from, and it matters more than it looks.
`archive` is ERA5 reanalysis and reaches back years; `forecast` is the operational model's
own past_days window and reaches back about 90 days. They are different models on different
grids - on 2026-08-02 they resolved to 52.337,5.167 and 52.366,5.22 for the same request.

Since capture_weather.py stores the forecast model going forward, `--via forecast` is the one
that gives a fit its history and its future from the same source. It covers every hour of
measured load this house has (the collector's own record starts 2026-07-17), so for the
question this data exists to answer, it is the better default of the two - it is not THE
default only because `archive` is what still works once the history outgrows 90 days.

This is the un-urgent half of the weather work, and deliberately so. Observations can be
re-fetched from a free API for ever: a run in 2027 returns the same numbers as a run today,
so nothing is lost by waiting and nothing is gained by rushing. The perishable half is
capture_weather.py, which records what the forecast SAID at the time - unreconstructable
once superseded, exactly like pv_forecast_raw_wh.

Re-running over a range already written is safe and expected. The observed series carries no
run tag, so InfluxDB overwrites point for point rather than accumulating duplicates. Widening
the range later, or re-fetching after Open-Meteo revises a day, needs no cleanup step.

Nothing consumes this data. It exists so that when the load forecast's remaining improvements
are exhausted and a temperature term finally comes up for judgement, the evidence to judge it
already exists rather than starting from zero on that day.

Exit codes: 0 wrote (or would have written) points, 1 ran correctly with nothing to write,
2 misconfigured or refused.
"""
import argparse
import sys
from datetime import datetime, timedelta

import influx_source as ix
import weather_source as ws

MAX_DAYS = 3650


def _parseDate(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError("must be YYYY-MM-DD, got %r" % value)


def _parseDays(value):
    try:
        days = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a whole number of days, got %r" % value)
    if days < 1 or days > MAX_DAYS:
        raise argparse.ArgumentTypeError("must be between 1 and %d, got %d" % (MAX_DAYS, days))
    return days


def _parseArgs(argv):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("days", nargs="?", type=_parseDays, metavar="DAYS",
                        help="how many days back from today (default 30)")
    parser.add_argument("--from", dest="start", type=_parseDate, metavar="YYYY-MM-DD",
                        help="first date to fetch, instead of counting days back")
    parser.add_argument("--to", dest="stop", type=_parseDate, metavar="YYYY-MM-DD",
                        help="last date to fetch (default: today)")
    parser.add_argument("--via", choices=("archive", "forecast"), default="archive",
                        help="which endpoint to read history from (default archive; see below)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the line protocol and write nothing")
    ns = parser.parse_args(argv)
    if ns.days is not None and (ns.start or ns.stop):
        parser.error("give either DAYS or --from/--to, not both - they mean the same thing")
    return ns


def resolveRange(ns, today):
    stop = ns.stop or today
    if ns.start:
        start = ns.start
    else:
        start = stop - timedelta(days=(ns.days or 30) - 1)
    return start, stop


def main(argv):
    ns = _parseArgs(argv)
    today = (datetime.now(ix.LOCAL_TZ) if ix.LOCAL_TZ else datetime.now()).date()
    start, stop = resolveRange(ns, today)
    if start > stop:
        print("Nothing to do: --from %s is after --to %s." % (start, stop))
        return 2

    # Checked before the fetch, not after, so an unconfigured run fails in a second without
    # troubling Open-Meteo for data it is about to throw away. --dry-run needs no database
    # and is allowed through, which also makes it the way to try this on a laptop.
    if not ns.dry_run and not ix.configured():
        c = ix.config()
        print("InfluxDB is not configured, so there is nowhere to write. Searched the")
        print("environment, then %s." % c["env_file"])
        print("Run 'python3 influx_source.py' to see what is missing, or pass --dry-run.")
        return 2

    try:
        if ns.via == "forecast":
            rows, nullHours, point = ws.fetchForecastPast(start, stop, today)
        else:
            rows, nullHours, point = ws.fetchArchive(start, stop)
    except Exception as exc:
        print("Could not fetch weather for %s -> %s: %s" % (start, stop, exc))
        return 2

    print("Weather backfill, %s -> %s, via the %s endpoint" % (start, stop, ns.via))
    for line in ws.describe(rows, nullHours, point):
        print("  " + line)

    if not rows:
        print()
        print("Nothing to write. The archive has no hours in that range - most likely it is")
        print("entirely in the future, or so recent the archive has not settled on it yet.")
        return 1

    lines = ws.linesFor(rows, ws.MEASUREMENT_OBSERVED)
    if ns.dry_run:
        print()
        for line in lines:
            print(line)
        print()
        print("--dry-run: %d points NOT written." % len(lines))
        return 0

    try:
        written = ix.writePoints(lines)
    except Exception as exc:
        print("Write failed: %s" % exc)
        return 2

    print("  wrote %d points to %s as %s"
          % (written, ix.config()["plan_bucket"], ws.MEASUREMENT_OBSERVED))
    print()
    print("Safe to re-run over the same range: no run tag means each point overwrites its")
    print("own earlier self rather than joining it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
