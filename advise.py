#!/usr/bin/env python3
"""Turn a Marstek-planning.py plan table into instructions a person can act on.

The planner emits one row per interval with charge/discharge/import/export in Wh. That is
the right shape for an optimiser and the wrong shape for a human: nobody wants 104 rows of
quarter-hours. This collapses consecutive intervals that call for the same action into
blocks, converts Wh per interval into the W setpoint the inverter actually takes, and
prices each block so the reason for it is visible.

    python3 advise.py plan_20260727_13.txt

With INFLUX_HOST set it also reads what the battery really did over the same window, so a
replayed plan can be held against the day that actually happened:

    INFLUX_HOST=192.168.68.105 python3 advise.py --actuals plan_20260727_13.txt

Actions are named from the battery's point of view. "Sell" means discharging to the grid;
"cover load" means discharging into the house, which earns the retail price rather than the
export price and is usually the more valuable of the two.
"""
import sys
from datetime import datetime, timedelta

COLS = ["date", "time", "pvD", "pvI", "use", "nett", "chrgD", "chrg", "dschg",
        "soc", "imp", "exp", "pr_buy", "pr_sell", "cost"]
CAPACITY_WH = 27900          # only used to print SoC as a percentage


def readPlan(path):
    rows = []
    with open(path) as f:
        for line in f:
            parts = line.split()
            if len(parts) != len(COLS) or parts[0] == "date":
                continue                      # header, or the ATTENTION warning line
            r = dict(zip(COLS, parts))
            for k in COLS[2:]:
                r[k] = float(r[k])
            r["ts"] = datetime.strptime(r["date"] + " " + r["time"], "%Y-%m-%d %H:%M")
            rows.append(r)
    rows.sort(key=lambda r: r["ts"])
    return rows


def intervalMinutes(rows):
    if len(rows) < 2:
        return 60
    return int((rows[1]["ts"] - rows[0]["ts"]).total_seconds() // 60)


def classify(r):
    """One action per interval. Order matters: grid charging outranks solar charging,
    because an interval doing both is one you must not miss."""
    charging = r["chrg"] > 1
    discharging = r["dschg"] > 1
    importing = r["imp"] > 1
    exporting = r["exp"] > 1
    if charging and importing:
        return "BUY", "CHARGE battery from grid"
    if charging:
        return "SOLAR", "charge battery from solar"
    if discharging and exporting:
        return "SELL", "SELL battery to grid"
    if discharging:
        return "COVER", "run house off battery"
    if exporting:
        return "SPILL", "export surplus solar"
    if importing:
        return "GRID", "run house off grid"
    return "IDLE", "battery idle"


def blocks(rows):
    out = []
    for r in rows:
        act, label = classify(r)
        if out and out[-1]["act"] == act:
            b = out[-1]
        else:
            b = {"act": act, "label": label, "rows": []}
            out.append(b)
        b["rows"].append(r)
    return out


def summarise(b, minutes):
    rows = b["rows"]
    perHour = 60.0 / minutes
    hours = len(rows) * minutes / 60.0
    key = {"BUY": "chrg", "SOLAR": "chrg", "SELL": "dschg",
           "COVER": "dschg", "SPILL": "exp", "GRID": "imp", "IDLE": None}[b["act"]]
    energy = sum(r[key] for r in rows) if key else 0.0
    peakW = max((r[key] * perHour for r in rows), default=0.0) if key else 0.0
    price = "pr_sell" if b["act"] in ("SELL", "SPILL") else "pr_buy"
    prices = [r[price] for r in rows]
    # weight the price by the energy in each interval, so a block that is mostly idle at a
    # high price does not read as if it all happened at that price
    wsum = sum(r[key] for r in rows) if key else 0.0
    avgPrice = (sum(r[key] * r[price] for r in rows) / wsum) if wsum else (
        sum(prices) / len(prices))
    return {
        "start": rows[0]["ts"],
        "end": rows[-1]["ts"] + timedelta(minutes=minutes),
        "hours": hours,
        "energy": energy,
        "peakW": peakW,
        "avgPrice": avgPrice,
        "minPrice": min(prices),
        "maxPrice": max(prices),
        "socEnd": rows[-1]["soc"],
        "cost": sum(r["cost"] for r in rows),
    }


def render(rows, title, showIdle=False):
    minutes = intervalMinutes(rows)
    print("=" * 96)
    print(title)
    print("  horizon %s -> %s   (%d intervals of %d min)" % (
        rows[0]["ts"].strftime("%a %d %b %H:%M"),
        (rows[-1]["ts"] + timedelta(minutes=minutes)).strftime("%a %d %b %H:%M"),
        len(rows), minutes))
    print("=" * 96)
    print("  from        to      action                        kWh   set W   ct/kWh  SoC end")
    print("  " + "-" * 76)
    for b in blocks(rows):
        if b["act"] in ("IDLE", "GRID") and not showIdle:
            continue
        s = summarise(b, minutes)
        print("  %s   %s   %-24s %6.2f  %6.0f  %6.1f     %3.0f%%" % (
            s["start"].strftime("%a %H:%M"),
            s["end"].strftime("%H:%M"),
            b["label"],
            s["energy"] / 1000.0,
            s["peakW"],
            s["avgPrice"] * 100,
            100.0 * s["socEnd"] / CAPACITY_WH))
    tot = {}
    for b in blocks(rows):
        s = summarise(b, minutes)
        t = tot.setdefault(b["act"], [0.0, 0.0, 0.0])
        t[0] += s["energy"] / 1000.0
        t[1] += s["cost"]
        # value of energy the battery supplied to the house: it is not a cash receipt, it
        # is a purchase that never happened, and at the retail price it is usually worth
        # more than exporting the same kWh
        if b["act"] == "COVER":
            t[2] += s["energy"] / 1000.0 * s["avgPrice"]
    print("  " + "-" * 76)
    cash = sum(v[1] for v in tot.values())
    avoided = sum(v[2] for v in tot.values())
    names = {"BUY": "bought into battery", "SOLAR": "solar into battery",
             "SELL": "sold to grid", "COVER": "house off battery",
             "SPILL": "solar exported", "GRID": "house off grid"}
    for act in ("BUY", "SOLAR", "SELL", "COVER", "SPILL", "GRID"):
        if act in tot and abs(tot[act][0]) > 0.005:
            extra = "   avoids %+6.2f EUR of buying" % tot[act][2] if act == "COVER" else ""
            print("  %-20s %7.2f kWh  %+7.2f EUR%s" % (
                names[act], tot[act][0], tot[act][1], extra))
    print("  " + "-" * 76)
    print("  grid cash flow over the horizon   %+7.2f EUR" % cash)
    print("  buying avoided by the battery     %+7.2f EUR" % avoided)
    print("  total benefit                     %+7.2f EUR" % (cash + avoided))
    print()


def actuals(rows):
    """What the battery really did over the plan's window, from InfluxDB."""
    try:
        import influx_source as ix
    except ImportError:
        print("  (actuals need influx_source.py)")
        return
    if not ix.configured():
        print("  (actuals need INFLUX_HOST and a token; run 'python3 influx_source.py')")
        return
    minutes = intervalMinutes(rows)
    start = rows[0]["ts"].replace(tzinfo=ix.LOCAL_TZ)
    stop = (rows[-1]["ts"] + timedelta(minutes=minutes)).replace(tzinfo=ix.LOCAL_TZ)
    batt = ix.hourlyEnergyWh(ix.FIELD_BATTERY, start, stop)
    grid = ix.hourlyEnergyWh(ix.FIELD_GRID, start, stop)
    if not batt:
        print("  (no measured data in this window)")
        return
    # sign conventions confirmed against a balancing sample: battery negative = charging,
    # grid positive = import
    chg = -sum(v for v in batt.values() if v < 0) / 1000.0
    dis = sum(v for v in batt.values() if v > 0) / 1000.0
    imp = sum(v for v in grid.values() if v > 0) / 1000.0
    exp = -sum(v for v in grid.values() if v < 0) / 1000.0
    print("  what actually happened over the same window (InfluxDB, %d hours):" % len(batt))
    print("    battery charged   %6.2f kWh" % chg)
    print("    battery discharged%6.2f kWh" % dis)
    print("    grid import       %6.2f kWh" % imp)
    print("    grid export       %6.2f kWh" % exp)
    print()


if __name__ == "__main__":
    minHours = 0.0
    rawArgs = sys.argv[1:]
    if "--min-hours" in rawArgs:
        i = rawArgs.index("--min-hours")
        minHours = float(rawArgs[i + 1])
        rawArgs = rawArgs[:i] + rawArgs[i + 2:]
    args = [a for a in rawArgs if not a.startswith("--")]
    flags = {a for a in rawArgs if a.startswith("--")}
    if not args:
        print(__doc__)
        raise SystemExit(2)
    tooShort = False
    for path in args:
        rows = readPlan(path)
        if not rows:
            print("no plan rows in %s" % path)
            continue
        render(rows, path, showIdle="--all" in flags)
        if "--actuals" in flags:
            actuals(rows)
        if minHours:
            minutes = intervalMinutes(rows)
            span = (rows[-1]["ts"] + timedelta(minutes=minutes) - rows[0]["ts"]).total_seconds() / 3600.0
            if span + 1e-9 < minHours:
                print("  ERROR: horizon is only %.2fh, need >= %.1fh (stale/short price data?)" % (span, minHours))
                tooShort = True
    if tooShort:
        raise SystemExit(1)
