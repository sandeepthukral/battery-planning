"""Measure the PV forecast against what the roof actually made, split by sun elevation.

    python3 fit_pv_elevation.py [days]        # default 30

Phase 5 of the plan: `pvOverallCalibration` is still an unfitted 1.00, and
`pvElevationLossCurve` was fitted against the APsystems EMA hourly export, which puts
energy in the wrong hour. Elevation moves fastest at dawn and dusk, exactly where the curve
is steepest, so the hours that set its steep end are the hours that source data is worst at.

This reads `pv_forecast_raw_wh` -- the uncalibrated forecast.solar output, stored since
2026-07-30 -- against measured PV from the collector, and reports both halves:

    LEVEL   total measured / total forecast, which is what pvOverallCalibration multiplies
    SHAPE   the same ratio per elevation band, which is what pvElevationLossCurve encodes

Both must be fitted against the RAW forecast. The two knobs multiply forecast.solar's own
output, so forecast.solar has to be the reference. Substituting a clear-sky model of one's
own -- the obvious shortcut, since it needs no forecast history -- does not work: two
equally defensible clear-sky models, run against the same fortnight of measured data on
2026-07-30, disagreed by 0.54 to 6.56 in the bands below 20 degrees, against a curve whose
entire range is 0.80. Below 25 degrees that method measures the model, not the roof. See
NAS-DEPLOYMENT-PLAN.md, Phase 5.

An interval is compared against the plan in force for it -- the most recent run at or
before it, the same rule report_day.py uses -- so the forecast being judged is the freshest
one that existed, and this measures the model's bias rather than how forecasts decay with
lead time.
"""
import importlib.util
import math
import os
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import influx_source as ix
import report_day as rd
import solar


def planner():
    """The planner module, loaded by path because its filename has a hyphen in it.

    Imported rather than copied so the curve compared against is always the one
    actually in use. A copy here would drift, and would drift silently, since a wrong
    curve renders as a plausible number rather than an error.
    """
    spec = importlib.util.spec_from_file_location(
        "marstek_planning", os.path.join(HERE, "Marstek-planning.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BIN_DEG = 5


def checkAgreement(mod, when, tolerance=1.0):
    """The planner's hourly elevation against solar.elevation() at the same instant,
    via its own date/hour-string wrapper (see Marstek-planning.py's solarElevation()).

    CODE-REVIEW.md D5 removed this file's own copy of the elevation formula - both
    call sites now share solar.py's implementation, so a FORMULA disagreement can no
    longer happen. What can still drift is the planner's WRAPPER around it: date/hour
    string parsing, the 30-minute hour-midpoint, and the HTTP-resolved site location.
    This still catches that - a silent break there would move every band boundary and
    the fit would still look entirely plausible.
    """
    lat, lon = float(mod.siteLatitude), float(mod.siteLongitude)
    theirs = mod.solarElevation(when.strftime("%Y-%m-%d"), when.strftime("%H"))
    mine = solar.elevation(lat, lon, when.replace(minute=30, second=0, microsecond=0))
    return theirs, mine, abs(theirs - mine) <= tolerance


def elevationOf(lat, lon, when, minutes):
    """Elevation at the middle of the interval, not at its start -- a quarter-hour labelled
    by its start sits most of a bin away from where its energy was made."""
    return solar.elevation(lat, lon, when + timedelta(minutes=minutes / 2.0))


def collect(days):
    stop = datetime.now(ix.LOCAL_TZ)
    start = stop - timedelta(days=days)
    points = ix.planPoints(start, stop, "plan")
    if not points:
        return None
    chosen, _, runs = rd.inForcePlans(points)

    times = sorted(chosen)
    minutes = rd.intervalMinutes(set(times))
    measured = ix.intervalEnergyWh(ix.FIELD_PV, start, stop, minutes, clamp_negative=True)

    mod = planner()
    lat, lon = float(mod.siteLatitude), float(mod.siteLongitude)
    rows = []
    # Counted separately because this tool returns nothing for weeks after it is written,
    # and "the field is missing" and "no daylight has passed since the field started" need
    # to look different. Otherwise the first real failure is indistinguishable from waiting.
    withField = 0
    for t in times:
        raw = chosen[t].get("pv_forecast_raw_wh")
        act = measured.get(t)
        if raw is not None:
            withField += 1
        # Only intervals the sun was actually up in. A night interval contributes 0/0 and
        # would otherwise flood the lowest band with meaningless ratios.
        if raw is None or act is None or raw <= 0:
            continue
        rows.append((elevationOf(lat, lon, t, minutes), raw, act))
    return {"rows": rows, "runs": runs, "start": start, "stop": stop,
            "minutes": minutes, "mod": mod, "lat": lat, "lon": lon,
            "planned": len(times), "withField": withField}


def stderr(values):
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return math.sqrt(var / n)


def main(argv):
    days = int(argv[0]) if argv else 30
    if not ix.configured():
        print("InfluxDB is not configured; run 'python3 influx_source.py' to see what is missing.")
        return 2

    d = collect(days)
    if d is None:
        print("No plans stored in the last %d days at all." % days)
        return 1
    if not d["rows"]:
        print("Nothing to fit yet, over the last %d days." % days)
        print("  past intervals with a plan in force   : %d" % d["planned"])
        print("  of those, carrying pv_forecast_raw_wh : %d" % d["withField"])
        if d["withField"] == 0:
            print("\nThe raw field is written from 2026-07-30 onwards. Before that only the")
            print("calibrated product was stored, and the forecast's own error cannot be")
            print("separated from the corrections applied to it. Nothing to be done but wait.")
        else:
            print("\nThe field is arriving, but no interval yet has both a raw forecast above")
            print("zero and a measured value - i.e. no daylight has passed since it started.")
            print("Run this again after a full day.")
        return 1

    rows = d["rows"]
    totalRaw = sum(r[1] for r in rows)
    totalAct = sum(r[2] for r in rows)

    print("PV forecast against measured, %s -> %s"
          % (d["start"].strftime("%Y-%m-%d"), d["stop"].strftime("%Y-%m-%d")))
    print("%d intervals of %d minutes, from %d plan runs"
          % (len(rows), d["minutes"], d["runs"]))

    noon = d["stop"].replace(hour=12, minute=30, second=0, microsecond=0)
    theirs, mine, ok = checkAgreement(d["mod"], noon)
    print("elevation check at %s: planner %.2f deg, this script %.2f deg -> %s\n"
          % (noon.strftime("%Y-%m-%d %H:%M"), theirs, mine, "agree" if ok else "DISAGREE"))
    if not ok:
        print("Refusing to fit: the two elevation calculations disagree, so every band")
        print("boundary below is suspect and the numbers would look plausible anyway.")
        return 2

    # --- LEVEL -------------------------------------------------------------------------
    print("LEVEL")
    print("  forecast (raw)   %8.1f kWh" % (totalRaw / 1000.0))
    print("  measured         %8.1f kWh" % (totalAct / 1000.0))
    if totalRaw > 0:
        print("  measured/forecast   %6.3f" % (totalAct / totalRaw))
    print("  pvOverallCalibration is currently %.2f" % d["mod"].pvOverallCalibration)
    print("  NOTE: that ratio is not the new value. The elevation curve multiplies the")
    print("        same forecast, so fit the shape first or its error lands in the level.")

    # --- SHAPE -------------------------------------------------------------------------
    bins = {}
    for elev, raw, act in rows:
        if elev < 0:
            continue
        bins.setdefault(int(elev // BIN_DEG) * BIN_DEG, []).append((raw, act))

    print("\nSHAPE")
    print("  elev band      n    forecast   measured   ratio   +/-    curve says")
    print("  ---------------------------------------------------------------------")
    for lo in sorted(bins):
        pairs = bins[lo]
        rawSum = sum(p[0] for p in pairs)
        actSum = sum(p[1] for p in pairs)
        ratio = actSum / rawSum if rawSum > 0 else float("nan")
        # Per-interval ratios only where the forecast is big enough for the ratio to mean
        # something: a 2 Wh forecast against 20 Wh measured is a 10x error about nothing.
        perInterval = [a / r for r, a in pairs if r >= 20]
        se = stderr(perInterval)
        # The curve is evaluated directly on elevation rather than through
        # pvElevationCalibration(), which takes a date and hour and derives elevation itself.
        curveSays = solar.interpolate(d["mod"].pvElevationLossCurve, lo + BIN_DEG / 2.0)
        print("  %3d-%3d %8d %9.1f  %9.1f  %6.3f  %s   %5.2f"
              % (lo, lo + BIN_DEG, len(pairs), rawSum / 1000.0, actSum / 1000.0, ratio,
                 ("%5.3f" % se) if se is not None else "    -", curveSays))

    print("\n  'ratio' is measured/forecast with no calibration applied. The curve is")
    print("  normalised to its high-sun plateau, so compare the SHAPE of the two columns,")
    print("  not their absolute values - a constant offset between them is the level.")
    print("  'curve says' is the curve at the band's midpoint, while 'ratio' is weighted by")
    print("  the energy in the band. On the steep low-sun segments those are not the same")
    print("  number even when the curve is exactly right, because most of a band's energy")
    print("  sits at its top. Narrow the bands before reading much into a small difference.")

    # --- is there enough of it yet -----------------------------------------------------
    print()
    thin = [lo for lo in sorted(bins) if len(bins[lo]) < 30]
    if thin:
        print("TOO THIN to conclude in these bands (under 30 intervals): %s"
              % ", ".join("%d-%d" % (lo, lo + BIN_DEG) for lo in thin))
    print("Winter is the gap that matters: at 52.5N the sun never gets above about 14")
    print("degrees in late December, so the whole of a winter day sits in the bands the")
    print("curve corrects hardest. Those bands cannot be filled from summer data at all -")
    print("in summer they occur only at dawn and dusk, with the sun in the north-east and")
    print("north-west rather than the south, over different obstructions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
