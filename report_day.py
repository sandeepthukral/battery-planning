#!/usr/bin/env python3
"""Hold a day's stored plans against what actually happened.

    python3 report_day.py                 # yesterday
    python3 report_day.py 2026-07-30      # a specific local date
    python3 report_day.py --write         # also store the comparison as measurement plan_score

Three questions, kept apart in the output because they fail independently and mixing them
hides which one went wrong:

  1. MONEY     what the advice was worth, in euros, at the prices that actually applied
  2. OUTCOMES  how close the day came to the plan - SoC, grid import and export
  3. FORECAST  how good the inputs were - PV and load, forecast against measured

Nothing executes these plans. This is an advisory planner by design and the AlphaESS runs
its own self-consumption logic, so section 1 measures the distance between that behaviour
and the optimiser's advice. It does not measure how well the plan was followed, because it
was not followed at all.

Eight plans are made each day. An interval is judged against the plan that was in force for
it - the most recent plan_run at or before it - because that is what a person acting on the
advice would have been following. Holding the 02:05 run responsible for the evening would
score it on prices that were not published until 13:00.

Prices come off the stored plan points rather than being refetched, so a later price
revision cannot change a past verdict. That also settles saldering for free: price_sell was
written with the regime that applied to that interval, so a report spanning 1 January 2027
values exports on both sides of the boundary correctly without repeating the rule here.
"""
import os
import sys
from datetime import datetime, date, time, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import influx_source as ix

CAPACITY_WH = float(os.environ.get("BT_CAP", "27900"))
PLAN_MEASUREMENT = "plan"
SCORE_MEASUREMENT = "plan_score"


def dayBounds(day):
    """Local midnight to local midnight. Built from dates, not by adding 24 hours, because
    the March and October days are 23 and 25 hours long."""
    tz = ix.LOCAL_TZ
    return (datetime.combine(day, time(0), tzinfo=tz),
            datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz))


def inForcePlans(points):
    """For each interval, the plan that was in force for it.

    Returns (chosen, firstRun, runsSeen) where chosen maps interval start -> plan point.
    An interval earlier than every plan_run of the day has no plan in force and is left out
    rather than scored against a plan made after the fact.
    """
    byTime = {}
    runTimes = {}
    for p in points:
        run = p["plan_run"]
        if run not in runTimes:
            try:
                runTimes[run] = ix._parse_time(run)
            except ValueError:
                continue
        byTime.setdefault(p["time"], {})[run] = p
    chosen = {}
    for t, runs in byTime.items():
        eligible = [r for r in runs if r in runTimes and runTimes[r] <= t]
        if eligible:
            chosen[t] = runs[max(eligible, key=lambda r: runTimes[r])]
    firstRun = min(runTimes.values()) if runTimes else None
    return chosen, firstRun, len(runTimes)


def intervalMinutes(times):
    """Plan resolution, read off the stored points rather than assumed."""
    times = sorted(times)
    gaps = {int((b - a).total_seconds() // 60) for a, b in zip(times, times[1:])}
    gaps = {g for g in gaps if g > 0}
    return min(gaps) if gaps else 60


def meterCost(importWh, exportWh, priceBuy, priceSell):
    """Euros at the meter for one interval. Positive means money out."""
    return importWh / 1000.0 * priceBuy - exportWh / 1000.0 * priceSell


def fmtDelta(value, unit, betterWhenLower=True):
    if abs(value) < 0.005:
        return "same"
    word = ("cheaper" if value < 0 else "dearer") if betterWhenLower else (
        "lower" if value < 0 else "higher")
    return "%+.2f %s  %s" % (value, unit, word)


def collect(day):
    """Everything the report needs, at the plan's own resolution."""
    start, stop = dayBounds(day)
    points = ix.planPoints(start, stop, PLAN_MEASUREMENT)
    if not points:
        return None
    chosen, firstRun, runsSeen = inForcePlans(points)
    minutes = intervalMinutes({p["time"] for p in points})

    grid = ix.intervalEnergyWh(ix.FIELD_GRID, start, stop, minutes)
    pv = ix.intervalEnergyWh(ix.FIELD_PV, start, stop, minutes, clamp_negative=True)
    load = ix.intervalEnergyWh(ix.FIELD_LOAD, start, stop, minutes, clamp_negative=True)
    batt = ix.intervalEnergyWh(ix.FIELD_BATTERY, start, stop, minutes)
    soc = ix.intervalLastValue(ix.FIELD_SOC, start, stop, minutes)

    expected = int(round((stop - start).total_seconds() / 60 / minutes))
    rows = []
    for t in sorted(chosen):
        if t not in grid:
            continue
        p = chosen[t]
        # battery negative = charging, grid positive = import: the sign conventions
        # confirmed against a balancing sample, same as advise.py relies on
        rows.append({
            "time": t,
            "plan_run": p["plan_run"],
            "priceBuy": p.get("price_buy", 0.0),
            "priceSell": p.get("price_sell", 0.0),
            "planImport": p.get("import_wh", 0.0),
            "planExport": p.get("export_wh", 0.0),
            "planCharge": p.get("charge_wh", 0.0),
            "planDischarge": p.get("discharge_wh", 0.0),
            "planSoc": p.get("soc_wh"),
            "planPv": p.get("pv_forecast_wh"),
            "planPvRaw": p.get("pv_forecast_raw_wh"),
            "planLoad": p.get("load_forecast_wh"),
            "actImport": max(grid[t], 0.0),
            "actExport": max(-grid[t], 0.0),
            "actCharge": max(-batt.get(t, 0.0), 0.0) if t in batt else None,
            "actDischarge": max(batt.get(t, 0.0), 0.0) if t in batt else None,
            "actSoc": (soc[t] / 100.0 * CAPACITY_WH) if t in soc else None,
            "actPv": pv.get(t),
            "actLoad": load.get(t),
        })
    return {"day": day, "start": start, "stop": stop, "minutes": minutes,
            "rows": rows, "planned": len(chosen), "expected": expected,
            "firstRun": firstRun, "runsSeen": runsSeen,
            "actualIntervals": len(grid)}


def sectionHeader(d):
    minutes = d["minutes"]
    print("=" * 96)
    print("Day report   %s        (%d-minute intervals, Europe/Amsterdam)"
          % (d["day"].strftime("%a %d %b %Y"), minutes))
    print("  plans stored   : %d run(s); first at %s"
          % (d["runsSeen"], d["firstRun"].strftime("%H:%M") if d["firstRun"] else "-"))
    print("  plan in force  : %d of %d intervals" % (d["planned"], d["expected"]))
    print("  actuals        : %d of %d intervals have enough samples"
          % (d["actualIntervals"], d["expected"]))
    print("  scored         : %d intervals (%.2f h) where both exist"
          % (len(d["rows"]), len(d["rows"]) * minutes / 60.0))
    print("=" * 96)
    if d["planned"] < d["expected"]:
        missing = d["expected"] - d["planned"]
        print("  NOTE: %d interval(s) had no plan in force - the first plan of the day was made"
              % missing)
        print("        at %s, and an interval is never judged against a plan written after it."
              % (d["firstRun"].strftime("%H:%M") if d["firstRun"] else "?"))
    if d["actualIntervals"] < d["expected"]:
        print("  NOTE: %d interval(s) fell below the sample-coverage threshold and are treated as"
              % (d["expected"] - d["actualIntervals"]))
        print("        missing, not as a quiet house. This day is incomplete, not cheap.")
    print()


def sectionMoney(rows):
    print("1. MONEY - what the advice was worth")
    print("   " + "-" * 89)
    if not rows:
        print("   nothing scored.\n")
        return {}
    planImp = sum(r["planImport"] for r in rows) / 1000.0
    planExp = sum(r["planExport"] for r in rows) / 1000.0
    actImp = sum(r["actImport"] for r in rows) / 1000.0
    actExp = sum(r["actExport"] for r in rows) / 1000.0
    planCost = sum(meterCost(r["planImport"], r["planExport"], r["priceBuy"], r["priceSell"]) for r in rows)
    actCost = sum(meterCost(r["actImport"], r["actExport"], r["priceBuy"], r["priceSell"]) for r in rows)

    # A third leg the plan document does not ask for, and the only one that makes the other
    # two readable: what the meter would have done with no battery at all, from the measured
    # load and PV. Without it a cheap day and a good plan are indistinguishable.
    baseRows = [r for r in rows if r["actLoad"] is not None and r["actPv"] is not None]
    baseCost = baseImp = baseExp = None
    if baseRows:
        baseImp = sum(max(r["actLoad"] - r["actPv"], 0.0) for r in baseRows) / 1000.0
        baseExp = sum(max(r["actPv"] - r["actLoad"], 0.0) for r in baseRows) / 1000.0
        baseCost = sum(meterCost(max(r["actLoad"] - r["actPv"], 0.0),
                                 max(r["actPv"] - r["actLoad"], 0.0),
                                 r["priceBuy"], r["priceSell"]) for r in baseRows)

    print("   %-30s %10s %10s %12s" % ("", "import", "export", "meter cost"))
    print("   %-30s %6.2f kWh %6.2f kWh %8.2f EUR" % ("as planned", planImp, planExp, planCost))
    print("   %-30s %6.2f kWh %6.2f kWh %8.2f EUR" % ("as it happened", actImp, actExp, actCost))
    if baseCost is not None:
        label = "with no battery at all"
        if len(baseRows) < len(rows):
            label += " (%d int.)" % len(baseRows)
        print("   %-30s %6.2f kWh %6.2f kWh %8.2f EUR" % (label, baseImp, baseExp, baseCost))
    print("   " + "-" * 89)
    print("   actual vs advised                    %s" % fmtDelta(actCost - planCost, "EUR"))
    if baseCost is not None:
        print("   the battery actually saved           %+.2f EUR" % (baseCost - actCost))
        print("   the plan said it could save          %+.2f EUR" % (baseCost - planCost))
    print()
    carry = socCarry(rows)
    if carry is not None:
        planDelta, actDelta = carry
        print("   Stored energy crossing the window: SoC %s %.2f kWh in the plan and %s %.2f kWh"
              % ("rose" if planDelta >= 0 else "fell", abs(planDelta) / 1000.0,
                 "rose" if actDelta >= 0 else "fell", abs(actDelta) / 1000.0))
        print("   in reality. Energy discharged here was bought before the window and energy")
        print("   left in the battery will be sold after it, so these euros are only a full")
        print("   answer when the window is a whole day and SoC ends near where it started.")
        print()
    print("   Read this as advice versus behaviour, not as execution error: nothing followed")
    print("   the plan. The gap is the distance between the AlphaESS's own logic and the")
    print("   optimiser, which is the number that says whether acting on the advice is worth it.")
    print()
    return {"planCost": planCost, "actCost": actCost, "baseCost": baseCost}


def socCarry(rows):
    """How much stored energy the scored window started and ended with, planned and actual.

    A window that opens full and closes empty earns money it did not create: that energy was
    bought before the window opened. Without this line a partial day reads as a triumph.
    """
    socRows = [r for r in rows if r["actSoc"] is not None and r["planSoc"] is not None]
    if len(socRows) < 2:
        return None
    return (socRows[-1]["planSoc"] - socRows[0]["planSoc"],
            socRows[-1]["actSoc"] - socRows[0]["actSoc"])


def sectionOutcomes(rows):
    print("2. OUTCOMES - how close the day came to the plan")
    print("   " + "-" * 89)
    if not rows:
        print("   nothing scored.\n")
        return
    planChg = sum(r["planCharge"] for r in rows) / 1000.0
    planDis = sum(r["planDischarge"] for r in rows) / 1000.0
    chgRows = [r for r in rows if r["actCharge"] is not None]
    actChg = sum(r["actCharge"] for r in chgRows) / 1000.0
    actDis = sum(r["actDischarge"] for r in chgRows) / 1000.0
    print("   battery charged      planned %6.2f kWh   actual %6.2f kWh   %s"
          % (planChg, actChg, fmtDelta(actChg - planChg, "kWh", False)))
    print("   battery discharged   planned %6.2f kWh   actual %6.2f kWh   %s"
          % (planDis, actDis, fmtDelta(actDis - planDis, "kWh", False)))

    socRows = [r for r in rows if r["actSoc"] is not None and r["planSoc"] is not None]
    if not socRows:
        print("   no interval has both a planned and a measured SoC.\n")
        return
    last = socRows[-1]
    print("   SoC at %s          planned %5.0f%%       actual %5.0f%%       %+.0f points"
          % (last["time"].strftime("%H:%M"),
             100.0 * last["planSoc"] / CAPACITY_WH,
             100.0 * last["actSoc"] / CAPACITY_WH,
             100.0 * (last["actSoc"] - last["planSoc"]) / CAPACITY_WH))
    worst = max(socRows, key=lambda r: abs(r["actSoc"] - r["planSoc"]))
    print("   widest gap at %s   planned %5.0f%%       actual %5.0f%%       %+.0f points"
          % (worst["time"].strftime("%H:%M"),
             100.0 * worst["planSoc"] / CAPACITY_WH,
             100.0 * worst["actSoc"] / CAPACITY_WH,
             100.0 * (worst["actSoc"] - worst["planSoc"]) / CAPACITY_WH))
    print()
    print("   hour   planned SoC   actual SoC    import pl/act (kWh)   export pl/act (kWh)")
    for hour, group in hourly(socRows).items():
        endOfHour = group[-1]
        print("   %s      %5.0f%%        %5.0f%%        %5.2f / %5.2f        %5.2f / %5.2f"
              % (hour,
                 100.0 * endOfHour["planSoc"] / CAPACITY_WH,
                 100.0 * endOfHour["actSoc"] / CAPACITY_WH,
                 sum(r["planImport"] for r in group) / 1000.0,
                 sum(r["actImport"] for r in group) / 1000.0,
                 sum(r["planExport"] for r in group) / 1000.0,
                 sum(r["actExport"] for r in group) / 1000.0))
    print()


def hourly(rows):
    out = {}
    for r in rows:
        out.setdefault(r["time"].strftime("%H:00"), []).append(r)
    return {k: out[k] for k in sorted(out)}


def sectionForecast(rows):
    print("3. FORECAST - how good the inputs were")
    print("   " + "-" * 89)
    pvRows = [r for r in rows if r["actPv"] is not None and r["planPv"] is not None]
    loadRows = [r for r in rows if r["actLoad"] is not None and r["planLoad"] is not None]
    if not pvRows and not loadRows:
        print("   nothing scored.\n")
        return
    rawRows = [r for r in pvRows if r.get("planPvRaw") is not None]
    if pvRows:
        f = sum(r["planPv"] for r in pvRows) / 1000.0
        a = sum(r["actPv"] for r in pvRows) / 1000.0
        print("   PV     forecast %6.2f kWh   measured %6.2f kWh   %s"
              % (f, a, pctErr(f, a)))
        if rawRows:
            # The planning forecast is the raw one times three corrections. Reporting only the
            # product cannot say whether a miss was forecast.solar's or the calibration's, and
            # those call for opposite fixes.
            rf = sum(r["planPvRaw"] for r in rawRows) / 1000.0
            ra = sum(r["actPv"] for r in rawRows) / 1000.0
            print("   PV raw forecast %6.2f kWh   measured %6.2f kWh   %s   (before calibration)"
                  % (rf, ra, pctErr(rf, ra)))
    if loadRows:
        f = sum(r["planLoad"] for r in loadRows) / 1000.0
        a = sum(r["actLoad"] for r in loadRows) / 1000.0
        print("   load   forecast %6.2f kWh   measured %6.2f kWh   %s"
              % (f, a, pctErr(f, a)))
    if pvRows:
        print()
        header = "   PV by hour (Wh)   forecast   measured   error"
        if rawRows:
            header += "          raw   raw error"
        print(header)
        for hour, group in hourly(pvRows).items():
            f = sum(r["planPv"] for r in group)
            a = sum(r["actPv"] for r in group)
            if f < 1 and a < 1:
                continue
            line = "   %s              %8.0f   %8.0f   %-12s" % (hour, f, a, pctErr(f, a))
            withRaw = [r for r in group if r.get("planPvRaw") is not None]
            if withRaw:
                rf = sum(r["planPvRaw"] for r in withRaw)
                line += " %8.0f   %s" % (rf, pctErr(rf, sum(r["actPv"] for r in withRaw)))
            print(line)
        print()
        print("   A daily total alone would hide the failure mode already seen once: on")
        print("   2026-07-29 forecast.solar missed a sunny evening by 43%, concentrated in a")
        print("   few hours rather than spread across the day.")
    print()
    print("   pv_forecast_wh is the PLANNING forecast, not the raw forecast.solar number: it")
    print("   carries the elevation calibration, pvOverallCalibration, and the deliberate 0.85")
    print("   conservatism factor. A steady under-forecast here is partly that factor working")
    print("   as intended. Fitting pvOverallCalibration means reading this with that in mind.")
    print()


def pctErr(forecast, actual):
    if actual <= 0.0001:
        return "no measured output"
    err = 100.0 * (forecast - actual) / actual
    return "%+.0f%% %s" % (err, "over" if err > 0 else "under")


def scoreLines(rows):
    """The per-interval comparison as line protocol, so a panel can query it instead of
    recomputing the same arithmetic in Flux and disagreeing with this file."""
    lines = []
    for r in rows:
        fields = {
            "import_wh_plan": r["planImport"], "import_wh_actual": r["actImport"],
            "export_wh_plan": r["planExport"], "export_wh_actual": r["actExport"],
            "cost_eur_plan": meterCost(r["planImport"], r["planExport"], r["priceBuy"], r["priceSell"]),
            "cost_eur_actual": meterCost(r["actImport"], r["actExport"], r["priceBuy"], r["priceSell"]),
            "soc_wh_plan": r["planSoc"], "soc_wh_actual": r["actSoc"],
            "pv_wh_forecast": r["planPv"], "pv_wh_actual": r["actPv"],
            "load_wh_forecast": r["planLoad"], "load_wh_actual": r["actLoad"],
        }
        if r["actLoad"] is not None and r["actPv"] is not None:
            fields["cost_eur_nobattery"] = meterCost(
                max(r["actLoad"] - r["actPv"], 0.0), max(r["actPv"] - r["actLoad"], 0.0),
                r["priceBuy"], r["priceSell"])
        # No plan_run tag. It would be the obvious thing to record, and it would add one
        # series per run for ever - the same cardinality trap the plan measurement already
        # carries once. The score is a single series; which run was judged stays in the text.
        lines.append(ix.linePoint(SCORE_MEASUREMENT, {}, fields, r["time"]))
    return lines


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    flags = {a for a in argv if a.startswith("--")}
    if "--help" in flags or "-h" in argv:
        print(__doc__)
        return 0
    if args:
        try:
            day = datetime.strptime(args[0], "%Y-%m-%d").date()
        except ValueError:
            print("usage: report_day.py [YYYY-MM-DD] [--write]")
            return 2
    else:
        day = (datetime.now(ix.LOCAL_TZ) - timedelta(days=1)).date()

    if not ix.configured():
        print("InfluxDB is not configured; run 'python3 influx_source.py' to see what is missing.")
        return 2

    d = collect(day)
    if d is None:
        print("No plans stored for %s in bucket %s." % (day, ix.config()["plan_bucket"]))
        print("Nothing to report - this is a day the planner did not run, not a bad day.")
        return 1

    sectionHeader(d)
    sectionMoney(d["rows"])
    sectionOutcomes(d["rows"])
    sectionForecast(d["rows"])

    if "--write" in flags and d["rows"]:
        try:
            written = ix.writePoints(scoreLines(d["rows"]))
            print("stored %d %s intervals in bucket %s"
                  % (written, SCORE_MEASUREMENT, ix.config()["plan_bucket"]))
        except Exception as e:
            # Same call as the planner makes about its own write: the report is already
            # printed, and losing the stored copy costs a panel, not the answer.
            print("WARNING: could not store the score (%s). The report above is unaffected." % e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
