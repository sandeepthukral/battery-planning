"""Record the weather forecast as it stands right now, as measurement weather_forecast.

    python3 capture_weather.py [--days 3] [--dry-run]

The perishable half of the weather work. Observations can be re-fetched from a free API for
ever, so backfill_weather.py has no deadline. A forecast cannot: nothing can reconstruct what
the model said at 14:05 last Tuesday once 17:05 has overwritten it. This is the same reason
pv_forecast_raw_wh started being stored on 2026-07-30 rather than being derived later.

Each run is tagged weather_run with the instant it was made, exactly as plans are tagged
plan_run, so report_day.py's "plan in force" rule applies unchanged: an hour is judged against
the most recent forecast at or before it. Without that tag the series would say what the
weather was going to be but not when anyone believed it, which answers nothing.

Meant to run beside the planner on its existing three-hourly schedule (scripts/plan.sh), and
to be non-fatal there. Nothing consumes this data and nothing will until a temperature term in
the load forecast is actually justified - so a weather outage must never hold up a plan.

Exit codes: 0 wrote points, 1 ran correctly with nothing to write, 2 misconfigured or refused.
"""
import argparse
import sys

import influx_source as ix
import weather_source as ws


def _parseArgs(argv):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=3, metavar="N",
                        help="forecast days to store (default 3; the plan horizon is ~48h)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the line protocol and write nothing")
    ns = parser.parse_args(argv)
    if ns.days < 1 or ns.days > 16:
        parser.error("--days must be between 1 and 16 (Open-Meteo's own limit)")
    return ns


def main(argv):
    ns = _parseArgs(argv)
    if not ns.dry_run and not ix.configured():
        print("InfluxDB is not configured, so there is nowhere to write. Searched the")
        print("environment, then %s." % ix.config()["env_file"])
        return 2

    run = ws.runStamp()
    try:
        rows, nullHours, point = ws.fetchForecast(pastDays=0, forecastDays=ns.days)
    except Exception as exc:
        print("Could not fetch the forecast: %s" % exc)
        return 2

    print("Weather forecast capture, run %s" % run)
    for line in ws.describe(rows, nullHours, point):
        print("  " + line)

    if not rows:
        print("Nothing to write.")
        return 1

    lines = ws.linesFor(rows, ws.MEASUREMENT_FORECAST, run=run)
    if ns.dry_run:
        for line in lines:
            print(line)
        print("--dry-run: %d points NOT written." % len(lines))
        return 0

    try:
        written = ix.writePoints(lines)
    except Exception as exc:
        print("Write failed: %s" % exc)
        return 2

    print("  wrote %d points to %s as %s, tagged weather_run=%s"
          % (written, ix.config()["plan_bucket"], ws.MEASUREMENT_FORECAST, run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
