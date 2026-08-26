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

Eight to twelve plans are made each day, and sections 1 and 2 judge the day against ONE of
them: the latest run that already covered the window's first interval - the plan a follower
would have woken up on. Not, as this report did until 2026-08-26, against whichever run was
in force at each interval.

That chain looked obviously right and was not. Every run restarts from the MEASURED SoC, so
chaining the in-force slices sums the opening moves of a dozen plans, each believing it had
a battery the previous one had not yet spent. On 2026-08-21 it produced a "plan" exporting
25.02 kWh where no single run exceeded 14.15, discharging 29.38 kWh out of a 27.9 kWh
battery, and claiming 7.54 EUR of advice against the 2.41 the committed plan actually
offered - while the day itself earned 5.82. The headline was not just wrong, it pointed the
wrong way. report_window.py's docstring had described the mechanism for months; this report
was simply not applying it.

The chain is still computed and printed, under section 1, as the diagnostic upper bound it
always was.

Prices come off the stored plan points rather than being refetched, so a later price
revision cannot change a past verdict. That also settles saldering for free: price_sell was
written with the regime that applied to that interval, so a report spanning 1 January 2027
values exports on both sides of the boundary correctly without repeating the rule here.
"""
import argparse
import os
import sys
from datetime import datetime, date, time, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import influx_source as ix
import hardware

# BT_CAP still overrides for a backtest/what-if run, same as planner.py's own
# getUserInput(); the DEFAULT comes from hardware.py (CODE-REVIEW.md D4) instead of a
# second hardcoded "27900" that could silently drift from the planner's own constant.
CAPACITY_WH = float(os.environ.get("BT_CAP", str(hardware.CAPACITY_WH)))
PLAN_MEASUREMENT = "plan"
SCORE_MEASUREMENT = "plan_score"

# How far a scored case's energy may fail to add up before the report refuses to publish it.
# The band is deliberately asymmetric, because the two directions are not the same claim.
#
# Positive is loss: energy left the battery and less of it reached the meter. At 85% RTE a
# hard-cycling day lands several percent positive and is perfectly healthy, so that side is
# loose. Negative is energy no source in the window supplied - a defect in the scoring, not
# a fact about the battery - so that side allows only what integer soc_wh and the direct-PV
# term can explain. The in-force stitch this file used to publish sat at -21%.
BALANCE_LOSS_TOLERANCE = 0.15
BALANCE_GAIN_TOLERANCE = 0.03
BALANCE_FLOOR_WH = 500.0


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


def committedRun(points, start, stop, minutes):
    """The one plan a follower would have been on for this whole window.

    Selection is by COVERAGE, not by the plan_run tag, and that distinction is the whole
    point of the function. The runs fire at :55 and their stamps land at :05 past the hour,
    so a tag-based cut at local midnight silently prefers the 22:55 run to the 23:55 one -
    an hour of extra staleness, in the direction nobody would notice. A run holding a point
    for the window's FIRST interval was made before the window opened, whatever its tag
    says and whatever the schedule changes to next. The tag then only orders the survivors,
    which it does correctly under either reading.

    planPoints() returns only points inside the window, so a run made before the window
    opens has min(times) == start, and one made during it does not. That is the test.

    Falls back to the best-covered eligible run when none spans the whole window: a day the
    planner started late, or one with a gap in the middle, still deserves a report. The
    caller prints the coverage so a partial basis is never mistaken for a whole day.

    Returns (plan_run, intervals covered), or (None, 0) when every run postdates the window.
    """
    expected = int(round((stop - start).total_seconds() / 60 / minutes))
    byRun = {}
    for p in points:
        if p["plan_run"]:
            byRun.setdefault(p["plan_run"], set()).add(p["time"])
    eligible = {}
    for run, times in byRun.items():
        if min(times) > start:
            continue
        try:
            ix._parse_time(run)
        except ValueError:
            continue
        eligible[run] = len(times)
    if not eligible:
        return None, 0
    full = [r for r in eligible if eligible[r] >= expected]
    if full:
        best = max(full, key=ix._parse_time)
    else:
        best = max(eligible, key=lambda r: (eligible[r], ix._parse_time(r)))
    return best, eligible[best]


def energyBalance(rows, side):
    """Whether a scored case's energy adds up, over the intervals that carry every term.

        pv + import + (SoC at the open - SoC at the close) - export - load  ~=  losses

    The check that would have caught the in-force stitch the day it shipped, instead of five
    months later: it printed a plan supplying 29.41 kWh and spending 41.43, a 12 kWh hole,
    under the heading "what the advice was worth". A case that cannot close this is not a
    plan, whatever else it is.

    Losses are one-sided, so a healthy day sits a few percent POSITIVE - energy leaves the
    battery and less of it reaches the meter. A negative residual is energy created out of
    nothing and is always a scoring defect.

    Energy is summed over rows[1:] against a drop measured from rows[0], the convention
    report_window.attribute() already uses: soc_wh is the level at the END of its interval,
    so the first row sets the opening level and contributes no energy of its own.

    Returns None when fewer than two intervals carry all five terms - a partial day is
    incomplete, not wrong, and must not be failed for it.
    """
    keys = {"plan": ("planPv", "planImport", "planExport", "planLoad", "planSoc"),
            "actual": ("actPv", "actImport", "actExport", "actLoad", "actSoc")}[side]
    pvK, impK, expK, loadK, socK = keys
    usable = [r for r in rows if all(r.get(k) is not None for k in keys)]
    if len(usable) < 2:
        return None
    spanning = usable[1:]
    pv = sum(r[pvK] for r in spanning)
    imp = sum(r[impK] for r in spanning)
    exp = sum(r[expK] for r in spanning)
    load = sum(r[loadK] for r in spanning)
    socDrop = usable[0][socK] - usable[-1][socK]
    residual = pv + imp + socDrop - exp - load
    throughput = pv + imp + exp + load
    fraction = BALANCE_LOSS_TOLERANCE if residual >= 0 else BALANCE_GAIN_TOLERANCE
    limit = max(BALANCE_FLOOR_WH, fraction * throughput)
    return {"side": side, "pv": pv, "import": imp, "export": exp, "load": load,
            "socDrop": socDrop, "residual": residual, "throughput": throughput,
            "limit": limit, "fraction": fraction, "ok": abs(residual) <= limit,
            "intervals": len(usable)}


def closingSocGap(rows):
    """(Wh the plan leaves in the battery above what reality left, the price it closes at).

    Two cases that end the window at different SoC have not been compared, they have been
    described. The plan of 2026-08-21 closed 1.38 kWh richer than the day did - half a euro
    of export it is simply holding until tomorrow, and the difference between "the advice
    was worth 2.41" and "2.96". The prose under section 1 has always said this; this returns
    the number so the report can say it in euros.
    """
    socRows = [r for r in rows if r["planSoc"] is not None and r["actSoc"] is not None]
    if not socRows:
        return None
    last = socRows[-1]
    return last["planSoc"] - last["actSoc"], last["priceSell"]


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
    """Everything the report needs for one local day."""
    start, stop = dayBounds(day)
    d = collectWindow(start, stop, committed=True)
    if d is not None:
        d["day"] = day
    return d


def collectWindow(start, stop, planRun=None, committed=False):
    """Everything the report needs, at the plan's own resolution.

    Split out of collect() so report_window.py can ask the same question of an arbitrary
    window without restating what an interval's row contains. One definition of a row means
    the day report and the window report cannot quietly disagree about, say, which sign of
    battery_power_w is a discharge.

    planRun pins every interval to one stored run instead of the in-force one, for a drift
    investigation: switching runs mid-window hides the drift, because every new run restarts
    from the measured SoC, so the gap being measured resets to zero at each seam.

    committed=True picks that run automatically - see committedRun() - and is what the day
    report uses. The same seam that hides drift also inflates money, and for the same
    reason. The in-force chain is still built and returned as rollingRows so section 1 can
    print it as the upper bound it is.
    """
    points = ix.planPoints(start, stop, PLAN_MEASUREMENT)
    if not points:
        return None
    minutes = intervalMinutes({p["time"] for p in points})
    rolling, firstRun, runsSeen = inForcePlans(points)
    runTag, coverage = None, 0
    if committed:
        runTag, coverage = committedRun(points, start, stop, minutes)
        chosen = {p["time"]: p for p in points if p["plan_run"] == runTag} if runTag else {}
    elif planRun is not None:
        pinned = [p for p in points if p["plan_run"] == planRun]
        if not pinned:
            return None
        chosen = {p["time"]: p for p in pinned}
        firstRun, runsSeen = ix._parse_time(planRun), 1
    else:
        chosen = rolling

    grid = ix.intervalEnergyWh(ix.FIELD_GRID, start, stop, minutes)
    pv = ix.intervalEnergyWh(ix.FIELD_PV, start, stop, minutes, clamp_negative=True)
    load = ix.intervalEnergyWh(ix.FIELD_LOAD, start, stop, minutes, clamp_negative=True)
    batt = ix.intervalEnergyWh(ix.FIELD_BATTERY, start, stop, minutes)
    soc = ix.intervalLastValue(ix.FIELD_SOC, start, stop, minutes)

    expected = int(round((stop - start).total_seconds() / 60 / minutes))

    def buildRows(byTime):
        rows = []
        for t in sorted(byTime):
            if t not in grid:
                continue
            p = byTime[t]
            # battery negative = charging, grid positive = import: the sign conventions
            # confirmed against a balancing sample, same as advise.py relies on
            rows.append(_row(p, t, grid, pv, load, batt, soc))
        return rows

    rows = buildRows(chosen)
    return {"start": start, "stop": stop, "minutes": minutes,
            "rows": rows, "planned": len(chosen), "expected": expected,
            "firstRun": firstRun, "runsSeen": runsSeen,
            "committedRun": runTag, "committedCoverage": coverage,
            "rollingRows": buildRows(rolling) if committed else None,
            "actualIntervals": len(grid)}


def _row(p, t, grid, pv, load, batt, soc):
    """One scored interval: what the plan said, and what the meters recorded."""
    return {
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
    }


def sectionHeader(d):
    minutes = d["minutes"]
    print("=" * 96)
    print("Day report   %s        (%d-minute intervals, Europe/Amsterdam)"
          % (d["day"].strftime("%a %d %b %Y"), minutes))
    print("  plans stored   : %d run(s); first at %s"
          % (d["runsSeen"], d["firstRun"].strftime("%H:%M") if d["firstRun"] else "-"))
    print("  scored against : %s"
          % (d["committedRun"] or "nothing - every stored run postdates this day"))
    print("  its coverage   : %d of %d intervals" % (d["planned"], d["expected"]))
    print("  actuals        : %d of %d intervals have enough samples"
          % (d["actualIntervals"], d["expected"]))
    print("  scored         : %d intervals (%.2f h) where both exist"
          % (len(d["rows"]), len(d["rows"]) * minutes / 60.0))
    print("=" * 96)
    if d["committedRun"] and d["planned"] < d["expected"]:
        print("  NOTE: no stored run covered the whole day, so the best-covered one is scored")
        print("        and %d interval(s) go unjudged. Sections 1 and 2 describe %.1f h, not 24."
              % (d["expected"] - d["planned"], d["planned"] * minutes / 60.0))
    if not d["committedRun"]:
        print("  NOTE: every stored run for this day begins after the day did - there is no plan")
        print("        a follower could have been on, and nothing here is judged against one.")
    if d["actualIntervals"] < d["expected"]:
        print("  NOTE: %d interval(s) fell below the sample-coverage threshold and are treated as"
              % (d["expected"] - d["actualIntervals"]))
        print("        missing, not as a quiet house. This day is incomplete, not cheap.")
    print()


def sectionMoney(d):
    rows = d["rows"]
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
    print("   %-30s %6.2f kWh %6.2f kWh %8.2f EUR"
          % ("as planned (one run)", planImp, planExp, planCost))
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
        gap = closingSocGap(rows)
        if gap is not None and abs(gap[0]) > 50.0:
            surplus, priceSell = gap
            credit = surplus / 1000.0 * priceSell
            print("   ...and closes %.2f kWh %s, worth %+.2f EUR at that interval's sell"
                  % (abs(surplus) / 1000.0,
                     "richer" if surplus > 0 else "emptier", credit))
            print("   like for like, the plan was worth    %+.2f EUR" % (baseCost - planCost + credit))
    print()
    sectionBound(d, baseCost)
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
    print("   Sections 1 and 2 are one run, scored end to end:")
    print("     %s" % (d["committedRun"] or "none available"))
    print("   They used to chain whichever run was in force at each interval, which spends the")
    print("   same battery as many times as there were plans that day.")
    print()
    print("   Read this as advice versus behaviour, not as execution error: nothing followed")
    print("   the plan. The gap is the distance between the AlphaESS's own logic and the")
    print("   optimiser, which is the number that says whether acting on the advice is worth it.")
    print()
    return {"planCost": planCost, "actCost": actCost, "baseCost": baseCost}


def sectionBound(d, baseCost):
    """The in-force chain, printed as the upper bound it always was.

    Kept rather than deleted because it does measure something: what the optimiser promised
    at each hour, given a battery it had not moved. That is a real diagnostic and a real
    ceiling on the advice. It is not a plan, its euros cannot be earned, and until
    2026-08-26 it was this report's headline.
    """
    rolling = d.get("rollingRows")
    if not rolling or baseCost is None:
        return
    rollCost = sum(meterCost(r["planImport"], r["planExport"], r["priceBuy"], r["priceSell"])
                   for r in rolling)
    print("   best-of-replans bound (diagnostic)   %+.2f EUR" % (baseCost - rollCost))
    print("   Not achievable, and not an alternative headline: every run restarts from the")
    print("   MEASURED SoC, so chaining their opening moves sums a dozen plans that each")
    print("   believed the battery was still full. A gap between this and the line above is")
    print("   the value of replanning that the battery never actually banked.")
    print()


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


def sectionOutcomes(d):
    rows = d["rows"]
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
    print("   Both SoC columns come from the one run named in the header, so this table can")
    print("   show a divergence. Scored against the in-force chain it could not: each hour's")
    print("   planned SoC came from a run seeded minutes earlier with the measured value, so")
    print("   the two columns tracked each other by construction and a 0-point close at 23:45")
    print("   meant nothing at all.")
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
    if loadRows:
        print()
        print("   load by hour (Wh)   forecast   measured   error")
        for hour, group in hourly(loadRows).items():
            f = sum(r["planLoad"] for r in group)
            a = sum(r["actLoad"] for r in group)
            if f < 1 and a < 1:
                continue
            print("   %s                %8.0f   %8.0f   %s" % (hour, f, a, pctErr(f, a)))
        print()
        print("   The load total above can be right while every hour in it is wrong: the")
        print("   forecast is one hour-of-day curve, so a morning over-estimate and an evening")
        print("   under-estimate of the same size cancel in the total and not in the plan.")
        print("   Read the evening rows first. calcTerminalReserveWh() sizes the overnight")
        print("   reserve from the forecast load in exactly those hours, with a flat 25%")
        print("   margin that does not know which hours the forecast is worst in.")
    print()
    print("   pv_forecast_wh is the PLANNING forecast, not the raw forecast.solar number: it")
    print("   carries the elevation calibration, pvOverallCalibration, and the deliberate 0.85")
    print("   conservatism factor. A steady under-forecast here is partly that factor working")
    print("   as intended. Fitting pvOverallCalibration means reading this with that in mind.")
    print()
    print("   load_forecast_wh has no such correction to allow for. It is the last 7 complete")
    print("   days of measured load, averaged per hour of day and weighted towards the recent")
    print("   ones - so it is the house predicting itself, and one day's error here says very")
    print("   little. fit_load_profile.py aggregates many days, which is where a bias shows.")
    print()


def sectionChecks(d):
    """Section 4. Whether the cases above are physically possible.

    Printed rather than merely asserted because the failure it guards is not a crash: the
    old report was internally consistent, well laid out, and wrong by 12 kWh. A reader had
    no way to tell. Returns True when the plan side closes, which main() uses to decide
    whether the report is fit to publish at all.
    """
    print("4. CHECKS - does the arithmetic close")
    print("   " + "-" * 89)
    ok = True
    for side, label in (("plan", "as planned"), ("actual", "as it happened")):
        b = energyBalance(d["rows"], side)
        if b is None:
            print("   %-16s not enough intervals carry every term - not checked" % label)
            continue
        print("   %-16s pv %6.2f + import %5.2f + SoC drop %6.2f - export %5.2f - load %5.2f"
              % (label, b["pv"] / 1000.0, b["import"] / 1000.0, b["socDrop"] / 1000.0,
                 b["export"] / 1000.0, b["load"] / 1000.0))
        print("   %-16s = %+.2f kWh unaccounted (%+.1f%% of throughput, limit %s%.0f%%)   %s"
              % ("", b["residual"] / 1000.0,
                 100.0 * b["residual"] / b["throughput"] if b["throughput"] else 0.0,
                 "+" if b["residual"] >= 0 else "-", 100.0 * b["fraction"],
                 "ok" if b["ok"] else "FAILED"))
        if side == "plan":
            ok = b["ok"]
    print()
    print("   Round-trip losses make a healthy day land a few percent POSITIVE: energy leaves")
    print("   the battery and less of it reaches the meter, so that side of the band is loose")
    print("   (%.0f%%). Negative is energy no source in the window supplied, so that side is not"
          % (100.0 * BALANCE_LOSS_TOLERANCE))
    print("   (%.0f%%). The chain this report used to score sat at -21%%."
          % (100.0 * BALANCE_GAIN_TOLERANCE))
    print()
    return ok


def pctErr(forecast, actual):
    if actual <= 0.0001:
        return "no measured output"
    err = 100.0 * (forecast - actual) / actual
    return "%+.0f%% %s" % (err, "over" if err > 0 else "under")


def scoreLines(rows):
    """The per-interval comparison as line protocol, so a panel can query it instead of
    recomputing the same arithmetic in Flux and disagreeing with this file.

    These rows are the committed run's, so the score dashboard now shows the same plan the
    text does. Every plan_score point written before 2026-08-26 came from the in-force chain
    and reads high - re-run those days to correct them, and note that a day whose committed
    run covers fewer intervals than the chain did leaves the surplus timestamps holding
    their old values until they are deleted.
    """
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


def _parseDate(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError("must be YYYY-MM-DD, got %r" % value)


def _parseArgs(argv):
    # CODE-REVIEW.md D9: replaces hand-rolled flag/positional splitting.
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("day", nargs="?", type=_parseDate, metavar="YYYY-MM-DD",
                         help="local date to report on (default: yesterday)")
    parser.add_argument("--write", action="store_true",
                         help="also store the comparison as measurement plan_score")
    return parser.parse_args(argv)


def main(argv):
    ns = _parseArgs(argv)
    day = ns.day or (datetime.now(ix.LOCAL_TZ) - timedelta(days=1)).date()

    if not ix.configured():
        print("InfluxDB is not configured; run 'python3 influx_source.py' to see what is missing.")
        return 2

    d = collect(day)
    if d is None:
        print("No plans stored for %s in bucket %s." % (day, ix.config()["plan_bucket"]))
        print("Nothing to report - this is a day the planner did not run, not a bad day.")
        return 1

    sectionHeader(d)
    sectionMoney(d)
    sectionOutcomes(d)
    sectionForecast(d["rows"])
    healthy = sectionChecks(d)

    if not healthy:
        # Refused, not warned. report.sh publishes on rc 0 and 1 and leaves the previous
        # report in place for anything else, so this is the one lever that stops a number
        # nobody can earn from being read as one that could - which is exactly what
        # happened for months. Nothing is written to plan_score either: a dashboard fed a
        # figure this report would not print is the same failure by another route.
        print("REFUSING TO PUBLISH: the planned case does not conserve energy (see section 4).")
        print("The report above is printed for diagnosis. No plan_score was written.")
        return 3

    if ns.write and d["rows"]:
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
