"""Turn a plan into the settings to type into the alphaess app, session by session.

The app trades on one global price pair - sell above X, buy below Y - and cannot be given a
schedule. The plan, meanwhile, is a solved horizon that follows no threshold at all. So the
only way to make the app act out the plan is to work backwards: for each stretch of planned
trading, find the threshold that reproduces exactly those trades, and retune the app when
that stretch ends.

"Exactly" is the whole problem, and it is why this is not simply the marginal price. A
threshold has two jobs: fire on every interval the plan trades in, and stay silent on every
interval it does not. Both are constraints, and they can conflict. For a sell session the
app sells whenever price > T, so

    T <  min(price over the intervals the plan sells in)     - or a planned sale is missed
    T >= max(price over the intervals it does not)           - or an unplanned sale happens

which admits a threshold only when the second maximum is below the first minimum. When the
window is empty no single number reproduces the plan, and the honest output is to say so
rather than emit a number that looks authoritative. Buy is the mirror: the app buys when
price < T, so T > max(over buying intervals) and T <= min(over the rest).

The "does not trade" set spans the whole time the setting is live - this session's start
until the next session of the same direction - not just the holes inside the session. That
is what actually happens: nobody edits the app overnight, so a price spike at 03:00 is
governed by the threshold entered at 20:00, and a check confined to the session would call
that setting exact while the battery quietly sold into it.

Sessions merge across gaps of up to GAP_INTERVALS, because a plan that sells for an hour,
pauses for one quarter and sells for another hour is one selling session to anyone holding
a phone. A gap containing the opposite trade is never merged across - that is a real
boundary, not a pause.

Prices here are raw market price in EUR/kWh (priceList[IDX_PRICE_KWH]), not the all-in
price the optimiser works in, because the app's bands are set against the market signal.
"""
import math
from datetime import timedelta

# Quarter-hours of inactivity that still count as one session. Three is a judgement call,
# not a derived number: on 2026-08-03 the plan drew seven selling bands, four of them a
# single quarter-hour, which cluster into the three sessions a human would name.
GAP_INTERVALS = 3

SELL = "sell"
BUY = "buy"

# Prices land on the 0.1 ct grid where possible, because that is what gets typed into a
# phone. 0.01 ct is the fallback for a window too narrow to hold a 0.1 ct step - rare, but
# a real threshold at an awkward value beats a round one that trades wrongly.
GRIDS = (0.001, 0.0001)

# Wh below which an interval is not doing the thing. Matches advise.py's classify().
FLOOR_WH = 1

EPS = 1e-9


def classifyInterval(row):
    """SELL, BUY or None for one interval.

    Both directions need two fields. charge/discharge are battery flows that do not say
    whether the energy came from the grid or the roof; import/export are whole-house meter
    flows that do not say whether the battery or the dishwasher moved it. Either alone is
    confidently wrong: import alone reads overnight house load as the plan buying, export
    alone reads midday PV spill as the plan selling. The battery trades with the grid only
    where both are non-zero.
    """
    charging = row["charge"] > FLOOR_WH
    discharging = row["discharge"] > FLOOR_WH
    importing = row["import"] > FLOOR_WH
    exporting = row["export"] > FLOOR_WH
    if charging and importing:
        return BUY
    if discharging and exporting:
        return SELL
    return None


def intervalMinutes(rows):
    if len(rows) < 2:
        return 60
    return int((rows[1]["ts"] - rows[0]["ts"]).total_seconds() // 60)


def _runsOf(kinds, action, gap):
    """Index runs of `action`, merged across gaps of at most `gap` other intervals."""
    other = BUY if action == SELL else SELL
    hits = [i for i, k in enumerate(kinds) if k == action]
    if not hits:
        return []
    runs = [[hits[0]]]
    for i in hits[1:]:
        between = kinds[runs[-1][-1] + 1:i]
        if len(between) <= gap and other not in between:
            runs[-1].append(i)
        else:
            runs.append([i])
    return runs


def _roundInside(target, lo, hi, loOpen, hiOpen):
    """The roundest number near `target` that still satisfies the bounds.

    `loOpen`/`hiOpen` say whether the bound itself is admissible, which differs by
    direction: a sell threshold may equal the highest non-selling price (the app needs
    price > T strictly) but must stay below the lowest selling one.
    """
    def ok(v):
        if lo is not None and (v <= lo + EPS if loOpen else v < lo - EPS):
            return False
        if hi is not None and (v >= hi - EPS if hiOpen else v > hi + EPS):
            return False
        return True

    for grid in GRIDS:
        for cand in (round(target / grid) * grid,
                     math.floor(target / grid) * grid,
                     math.ceil(target / grid) * grid):
            cand = round(cand, 6)
            if ok(cand):
                return cand
    return round(target, 6)


def _threshold(action, tradePrices, quietPrices):
    """Pick a threshold, and say whether it reproduces the plan exactly.

    Aims at the middle of the admissible window rather than either edge. The margin is not
    protection against the price moving - these are day-ahead prices and the app compares
    against the same published number - but against the two feeds disagreeing in the last
    decimal, which sitting exactly on a bound would turn into a wrong trade.
    """
    if action == SELL:
        edge, opposing = min(tradePrices), (max(quietPrices) if quietPrices else None)
        exact = opposing is None or opposing < edge - EPS
        if exact:
            target = (opposing + edge) / 2.0 if opposing is not None else edge - GRIDS[0]
            setTo = _roundInside(target, opposing, edge, loOpen=False, hiOpen=True)
        else:
            # No threshold satisfies both. Prefer catching every planned sale over
            # suppressing the unplanned ones: a missed sale is energy left in a battery
            # that the plan needed empty, while an extra sale is a trade at a price the
            # plan itself was willing to sell at.
            setTo = _roundInside(edge - GRIDS[0], None, edge, loOpen=False, hiOpen=True)
        extra = sum(1 for p in quietPrices if p > setTo + EPS)
    else:
        edge, opposing = max(tradePrices), (min(quietPrices) if quietPrices else None)
        exact = opposing is None or edge < opposing - EPS
        if exact:
            target = (edge + opposing) / 2.0 if opposing is not None else edge + GRIDS[0]
            setTo = _roundInside(target, edge, opposing, loOpen=True, hiOpen=False)
        else:
            setTo = _roundInside(edge + GRIDS[0], edge, None, loOpen=True, hiOpen=False)
        extra = sum(1 for p in quietPrices if p < setTo - EPS)
    return setTo, exact, extra


def appSettings(rows, gap=GAP_INTERVALS):
    """One setting per trading session, in time order.

    `rows` are the plan's intervals in time order, each a dict of ts, price (EUR/kWh,
    market), charge, discharge, import, export (Wh) and soc (Wh).

    Each result carries `setTo` (EUR/kWh), `exact`, and `extra` - the number of intervals
    that would trade against the plan's wishes when no exact threshold exists. `until` and
    `targetSocWh` describe the end of the session: when to change the setting, and what the
    battery should read by then.
    """
    if not rows:
        return []
    minutes = intervalMinutes(rows)
    kinds = [classifyInterval(r) for r in rows]
    out = []
    for action in (SELL, BUY):
        runs = _runsOf(kinds, action, gap)
        for n, run in enumerate(runs):
            # Live until the next session of the same direction takes over, or to the end
            # of the horizon. The app keeps applying it either way.
            liveEnd = runs[n + 1][0] if n + 1 < len(runs) else len(rows)
            trading = set(run)
            tradePrices = [rows[i]["price"] for i in run]
            quietPrices = [rows[i]["price"] for i in range(run[0], liveEnd)
                           if i not in trading]
            setTo, exact, extra = _threshold(action, tradePrices, quietPrices)
            last = rows[run[-1]]
            out.append({
                "action": action,
                "start": rows[run[0]]["ts"],
                "until": last["ts"] + timedelta(minutes=minutes),
                "setTo": setTo,
                "exact": exact,
                "extra": extra,
                "targetSocWh": last["soc"],
                "intervals": len(run),
                "energyWh": sum(rows[i]["charge" if action == BUY else "discharge"]
                                for i in run),
            })
    out.sort(key=lambda s: (s["start"], s["action"]))
    return out
