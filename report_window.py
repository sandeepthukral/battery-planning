#!/usr/bin/env python3
"""Why a planned and a measured SoC curve drifted apart, interval by interval.

    python3 report_window.py 2026-08-02T18:15 2026-08-02T22:15
    python3 report_window.py 2026-08-02T18:15 2026-08-02T22:15 --run 2026-08-02T18:05:00Z
    python3 report_window.py 2026-08-02T18:15 2026-08-02T22:15 --runs   # just list the runs

report_day.py answers "how far apart were they". This answers "why", which is a different
question and needs two things that report differs on.

FIRST, per-interval resolution. A drift of a few SoC points accumulated over four hours is
invisible in an hourly table and obvious in a quarter-hourly one.

SECOND, and the reason this file exists at all: report_day.py scores each interval against
the plan that was IN FORCE for it, which is right for judging advice and wrong for measuring
drift. Eight to twelve plans are made a day, and every new run starts from the MEASURED SoC.
So a gap that has been opening all evening is reset to zero by the next run, and the drift
being investigated disappears into the seam between two plans. --run pins one stored run and
follows it to the end of the window, which is what a person acting on that plan actually
experienced.

The attribution splits the drift into two causes that call for opposite responses:

  ENERGY    the battery moved less (or more) energy than the plan asked for. An operational
            miss - a late start, a cap, a wrong app setting.
  MODEL     the battery moved the energy the plan asked for, but its SoC did not fall by as
            much as the plan said it would. A modelling error, in the optimiser's own SoC
            recursion, that no amount of correct operation would remove.

Both are reported in Wh and in points of a full battery, because the first is what the
arithmetic produces and the second is what the app displays.
"""
import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import influx_source as ix
import report_day
from report_day import CAPACITY_WH, PLAN_MEASUREMENT

# The optimiser's own efficiency, derived the same way Marstek-planning.py:545 derives it, so
# a changed RTE moves both together. The SoC recursion (Marstek-planning.py:2006-2008) debits
# discharge/onewayEff and credits onewayEff*charge, which is what this file holds the measured
# battery against.
RTE = float(os.environ.get("BT_RTE", "85"))
ONEWAY_EFF = (100.0 - (100.0 - RTE) / 2.0) / 100.0


def pct(wh):
    return 100.0 * wh / CAPACITY_WH


def attribute(rows):
    """Split the planned-vs-actual SoC drift into an energy cause and a model cause.

    Returns None when fewer than two intervals carry both a planned and a measured SoC -
    drift is a difference between two points and does not exist for one.

    The split is exact rather than approximate, which is the point. The optimiser's
    recursion (Marstek-planning.py:2006-2008) is

        planDrop = planDischarge/eff - eff*planCharge - planPv

    so subtracting the measured drop from it and adding and subtracting the same actual-energy
    term gives three components that reconstruct the drift with nothing left over:

      energy  (planDischarge - actDischarge)/eff - eff*(planCharge - actCharge)
              the SoC the plan spent on energy the battery did not move. Operational.
      model   actDischarge/eff - eff*actCharge - actualDrop
              the SoC the plan's arithmetic would have taken off for the energy that WAS
              moved, minus what really came off. Pure modelling error.
      pv      -planPv, the forecast PV the plan credits straight to SoC.

    `rest` is carried anyway and printed when it is not ~0, because a non-zero value means
    the stored plan does not satisfy its own recursion - a real finding, not a rounding
    crumb, and one worth seeing rather than absorbing into a fudge term.
    """
    usable = [r for r in rows if r["actSoc"] is not None and r["planSoc"] is not None]
    if len(usable) < 2:
        return None
    # Drops are measured across the same span the plan's own energy figures cover: the first
    # row establishes the starting SoC and contributes no energy, exactly as the recursion
    # treats interval 0.
    spanning = usable[1:]
    planDrop = usable[0]["planSoc"] - usable[-1]["planSoc"]
    actDrop = usable[0]["actSoc"] - usable[-1]["actSoc"]

    planDis = sum(r["planDischarge"] for r in spanning)
    planChg = sum(r["planCharge"] for r in spanning)
    actDis = sum(r["actDischarge"] or 0.0 for r in spanning)
    actChg = sum(r["actCharge"] or 0.0 for r in spanning)

    planPv = sum(r["planPv"] or 0.0 for r in spanning)

    energy = (planDis - actDis) / ONEWAY_EFF - ONEWAY_EFF * (planChg - actChg)
    model = (actDis / ONEWAY_EFF - ONEWAY_EFF * actChg) - actDrop
    return {
        "from": usable[0]["time"], "to": usable[-1]["time"],
        "openGap": usable[0]["actSoc"] - usable[0]["planSoc"],
        "closeGap": usable[-1]["actSoc"] - usable[-1]["planSoc"],
        "drift": planDrop - actDrop,
        "energy": energy, "model": model, "pv": -planPv,
        "rest": (planDrop - actDrop) - energy - model + planPv,
        "planDrop": planDrop, "actDrop": actDrop,
        "planDischarge": planDis, "actDischarge": actDis,
        "planCharge": planChg, "actCharge": actChg,
        "planPv": planPv,
        "actPv": sum(r["actPv"] or 0.0 for r in spanning),
    }


def sectionIntervals(rows, minutes):
    print("PER INTERVAL   discharge and charge in Wh at the meter, SoC in %% of %.1f kWh"
          % (CAPACITY_WH / 1000.0))
    print("   " + "-" * 101)
    print("   time    plan dis  act dis  plan chg  act chg   plan SoC  act SoC    gap   "
          "plan drop  act drop")
    prev = None
    for r in rows:
        if r["planSoc"] is None or r["actSoc"] is None:
            continue
        planDrop = actDrop = None
        if prev is not None:
            planDrop = prev["planSoc"] - r["planSoc"]
            actDrop = prev["actSoc"] - r["actSoc"]
        print("   %s   %7.0f  %7s  %8.0f  %7s    %5.1f%%   %5.1f%%  %+5.1f   %8s  %8s"
              % (r["time"].strftime("%H:%M"),
                 r["planDischarge"],
                 "%.0f" % r["actDischarge"] if r["actDischarge"] is not None else "-",
                 r["planCharge"],
                 "%.0f" % r["actCharge"] if r["actCharge"] is not None else "-",
                 pct(r["planSoc"]), pct(r["actSoc"]),
                 pct(r["actSoc"] - r["planSoc"]),
                 "%.0f" % planDrop if planDrop is not None else "-",
                 "%.0f" % actDrop if actDrop is not None else "-"))
        prev = r
    print()


def sectionAttribution(a, minutes):
    print("ATTRIBUTION   %s to %s" % (a["from"].strftime("%H:%M"), a["to"].strftime("%H:%M")))
    print("   " + "-" * 101)
    print("   the gap opened at   %+6.1f points   (actual SoC above plan)" % pct(a["openGap"]))
    print("   the gap closed at   %+6.1f points" % pct(a["closeGap"]))
    print("   so it %-13s %+6.1f points   =  %+7.0f Wh of drift to explain"
          % ("WIDENED by" if a["drift"] > 0 else "narrowed by", pct(a["drift"]), a["drift"]))
    print()
    print("   %-34s %9s %9s" % ("", "Wh", "points"))
    print("   %-34s %9.0f %9.1f" % ("ENERGY the battery did not move", a["energy"], pct(a["energy"])))
    print("   %-34s %9.0f %9.1f" % ("MODEL mis-stating the SoC cost", a["model"], pct(a["model"])))
    print("   %-34s %9.0f %9.1f" % ("PV the plan credited to SoC", a["pv"], pct(a["pv"])))
    if abs(a["rest"]) > 1.0:
        # Expected to be small and non-zero: soc_wh is stored as an int, and the recursion's
        # PV term is the DIRECT-to-battery forecast while pv_forecast_wh is the whole-array
        # one. A residual that is a large share of the drift means something else is wrong.
        print("   %-34s %9.0f %9.1f  <- %.0f%% of the drift; int rounding and the direct-PV term"
              % ("unreconciled", a["rest"], pct(a["rest"]),
                 abs(100.0 * a["rest"] / a["drift"]) if a["drift"] else 0.0))
    print("   " + "-" * 101)
    print("   %-34s %9.0f %9.1f" % ("total", a["drift"], pct(a["drift"])))
    print()
    print("   battery discharged   plan %7.0f Wh   actual %7.0f Wh   %+.0f Wh"
          % (a["planDischarge"], a["actDischarge"], a["actDischarge"] - a["planDischarge"]))
    print("   battery charged      plan %7.0f Wh   actual %7.0f Wh   %+.0f Wh"
          % (a["planCharge"], a["actCharge"], a["actCharge"] - a["planCharge"]))
    print("   SoC fell             plan %7.0f Wh   actual %7.0f Wh   %+.0f Wh"
          % (a["planDrop"], a["actDrop"], a["actDrop"] - a["planDrop"]))
    print()
    print("   PV                   plan %7.0f Wh   actual %7.0f Wh   (forecast vs measured)"
          % (a["planPv"], a["actPv"]))
    print()
    print("   SoC moved per Wh at the meter          plan     measured")
    if a["actDischarge"] > 0:
        print("     discharging (SoC out per Wh out)   %7.3f      %7.3f"
              % (1.0 / ONEWAY_EFF, a["actDrop"] / a["actDischarge"]))
    if a["actCharge"] > 0:
        print("     charging    (SoC in  per Wh in)    %7.3f      %7.3f"
              % (ONEWAY_EFF, -a["actDrop"] / a["actCharge"]))
    print()
    print("   RTE is %.0f%%, so onewayEff is %.3f - the planner's default, overridden nowhere."
          % (RTE, ONEWAY_EFF))
    print("   Both measured columns are SoC as the battery REPORTS it, against energy at the")
    print("   meter. A discharge column below 1.000 is not over-unity: it means those two")
    print("   numbers are not measured on the same side of the inverter, or that soc_percent")
    print("   is scaled against a capacity other than the %.1f kWh assumed here."
          % (CAPACITY_WH / 1000.0))
    print()


def sectionRuns(points):
    runs = sorted({p["plan_run"] for p in points})
    print("PLAN RUNS covering this window")
    print("   " + "-" * 101)
    for run in runs:
        covered = sorted(p["time"] for p in points if p["plan_run"] == run)
        print("   %-26s %3d interval(s), %s to %s"
              % (run, len(covered),
                 covered[0].strftime("%H:%M"), covered[-1].strftime("%H:%M")))
    print()
    print("   Pin one with --run to follow it across the whole window. Without it every")
    print("   interval is scored against the run in force for it, and each new run starts")
    print("   from the measured SoC - which resets the very drift this report measures.")
    print()


def _parseTime(value):
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=ix.LOCAL_TZ)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        "must be local YYYY-MM-DDTHH:MM, got %r" % value)


def _parseArgs(argv):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("start", type=_parseTime, metavar="YYYY-MM-DDTHH:MM")
    parser.add_argument("stop", type=_parseTime, metavar="YYYY-MM-DDTHH:MM")
    parser.add_argument("--run", metavar="PLAN_RUN",
                        help="pin every interval to this stored run (see --runs)")
    parser.add_argument("--runs", action="store_true",
                        help="list the runs covering this window and stop")
    return parser.parse_args(argv)


def main(argv):
    ns = _parseArgs(argv)
    if ns.stop <= ns.start:
        print("stop must be after start.")
        return 2
    if not ix.configured():
        print("InfluxDB is not configured; run 'python3 influx_source.py' to see what is missing.")
        return 2

    if ns.runs:
        points = ix.planPoints(ns.start, ns.stop, PLAN_MEASUREMENT)
        if not points:
            print("No plans stored for that window.")
            return 1
        sectionRuns(points)
        return 0

    d = report_day.collectWindow(ns.start, ns.stop, planRun=ns.run)
    if d is None:
        print("No plans stored for that window%s."
              % (" under run %s" % ns.run if ns.run else ""))
        return 1

    print("=" * 104)
    print("Window report   %s to %s        (%d-minute intervals, Europe/Amsterdam)"
          % (ns.start.strftime("%a %d %b %H:%M"), ns.stop.strftime("%H:%M"), d["minutes"]))
    print("  plan          : %s" % (ns.run if ns.run else "whichever run was in force, per interval"))
    print("  runs stored   : %d covering this window" % d["runsSeen"])
    print("  scored        : %d interval(s) where a plan and an actual both exist" % len(d["rows"]))
    print("=" * 104)
    print()

    sectionIntervals(d["rows"], d["minutes"])
    a = attribute(d["rows"])
    if a is None:
        print("Fewer than two intervals carry both a planned and a measured SoC; no drift to split.")
        return 1
    sectionAttribution(a, d["minutes"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
