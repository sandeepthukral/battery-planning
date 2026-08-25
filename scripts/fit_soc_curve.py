#!/usr/bin/env python3
"""Re-derive soc_curve.WH_PER_PCT from `power_readings` in the alphaess bucket.

    python scripts/fit_soc_curve.py                    # last 30 days, print the table
    python scripts/fit_soc_curve.py --days 60
    python scripts/fit_soc_curve.py --validate         # also score it against whole runs

Prints a WH_PER_PCT list ready to paste into soc_curve.py, plus how many SoC points
of real movement stand behind each band. Bands with little or no coverage fall back
to the prior - the pack never goes below the reserve floor, so the bottom of the
curve can only ever be a guess, and it should be visible as one.

Needs numpy and scipy, which the planner itself does not: this is a once-in-a-while
maintenance script, not part of a planning run.

    pip install numpy scipy

Why the fit rather than bucketing samples: soc_percent moves in 0.4-point steps, so
most 30s samples show real energy flow against zero gauge movement. Attributing each
sample's energy to the band it sits in biases every band low, and averaging the gauge
to smooth it invents intermediate values that alias against the 0.4 grid. Instead each
pair of gauge steps inside one contiguous run gives an exact equation - measured energy
between two gauge readings - and the per-band densities are whatever set of numbers
best reproduces all of them at once.
"""
import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import influx_source

BAND_WIDTH_PCT = 5.0
NBANDS = int(100 / BAND_WIDTH_PCT)
MIN_POWER_W = 800.0      # below this the sign of the gauge's movement is mostly noise
MAX_GAP_S = 180.0        # a longer hole means the run is broken, not merely idle
GAUGE_STEP_PCT = 0.39    # the gauge moves in 0.4-point steps; anything less is the same reading
MIN_SPAN_PCT = 4.0       # equations shorter than this are dominated by gauge quantisation
PRIOR_WH_PER_PCT = 290.0

# soc_percent is a state, so it takes `last` and not `mean`: averaging a gauge that
# steps by 0.4 manufactures 0.2-point readings that never happened, and those alias
# into a periodic sawtooth across the bands. battery_power_w is a rate, so it takes
# `mean`. Same reason influx_source.hourlySeries treats the two differently.
FLUX_SERIES = """
from(bucket: "%(bucket)s")
  |> range(start: -%(days)dd)
  |> filter(fn: (r) => r._measurement == "power_readings" and r._field == "%(field)s")
  |> aggregateWindow(every: 1m, fn: %(fn)s, timeSrc: "_start", createEmpty: false)
  |> group() |> sort(columns: ["_time"]) |> keep(columns: ["_time", "_value"])
"""


def _series(days, field, fn):
    """One field as {timestamp: value}. Two cheap queries beat one Flux join here:
    joining a month of 1-minute windows server-side runs past http_config.HTTP_TIMEOUT,
    while each half on its own returns in a couple of seconds."""
    out = {}
    for r in influx_source._query(FLUX_SERIES % {"bucket": influx_source.config()["bucket"],
                                                 "days": days, "field": field, "fn": fn}):
        try:
            out[dt.datetime.fromisoformat(r["_time"].replace("Z", "+00:00"))] = float(r["_value"])
        except (ValueError, KeyError, TypeError):
            continue
    return out


def loadSamples(days):
    power = _series(days, "battery_power_w", "mean")
    soc = _series(days, "soc_percent", "last")
    return sorted((t, w, soc[t]) for t, w in power.items() if t in soc)


def dischargeRuns(samples):
    """Contiguous stretches where the pack discharged at a rate the planner would schedule."""
    runs, cur = [], []
    for i in range(1, len(samples)):
        t0, _, _ = samples[i - 1]
        t1, w1, _ = samples[i]
        if (t1 - t0).total_seconds() <= MAX_GAP_S and w1 > MIN_POWER_W:
            cur.append((samples[i - 1], t1))
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return runs


def buildEquations(runs, np):
    """One row per pair of gauge steps within a run: band overlaps -> measured Wh."""
    A, y = [], []
    for run in runs:
        cum, pts = 0.0, [(run[0][0][2], 0.0)]
        for (t0, w, soc), t1 in run:
            cum += w * (t1 - t0).total_seconds() / 3600.0
            if abs(soc - pts[-1][0]) >= GAUGE_STEP_PCT:
                pts.append((soc, cum))
        for i in range(len(pts)):
            for j in range(i + 1, len(pts)):
                hi, lo = max(pts[i][0], pts[j][0]), min(pts[i][0], pts[j][0])
                if hi - lo < MIN_SPAN_PCT:
                    continue
                row = np.zeros(NBANDS)
                for b in range(NBANDS):
                    row[b] = max(0.0, min(hi, (b + 1) * BAND_WIDTH_PCT) - max(lo, b * BAND_WIDTH_PCT))
                A.append(row)
                y.append(abs(pts[j][1] - pts[i][1]))
    return np.array(A), np.array(y)


def fit(A, y, np, nnls, smoothing):
    coverage = A.sum(axis=0)
    # Scale each equation by 1/span so a 60-point run does not outvote a 5-point one
    # simply for covering more ground.
    scale = 1.0 / np.maximum(A.sum(axis=1), 1e-9)
    blocks = [A * scale[:, None]]
    targets = [y * scale]

    # Second differences: thinly-sampled bands follow their neighbours rather than
    # chasing whatever handful of equations happens to touch them.
    D = np.zeros((NBANDS - 2, NBANDS))
    for k in range(NBANDS - 2):
        D[k, k], D[k, k + 1], D[k, k + 2] = 1.0, -2.0, 1.0
    blocks.append(smoothing * D)
    targets.append(np.zeros(NBANDS - 2))

    # Bands below the reserve floor carry no equations at all, and smoothing alone would
    # extrapolate them to wherever the trend points - which came out at 470 Wh/point, a
    # number with nothing behind it. Anchor every band to a neutral prior with a weight
    # that fades as coverage grows: well-observed bands ignore it, unvisited ones keep it.
    anchor = 3.0 / (1.0 + coverage / 2000.0)
    blocks.append(np.diag(anchor))
    targets.append(anchor * PRIOR_WH_PER_PCT)

    density, _ = nnls(np.vstack(blocks), np.concatenate(targets))
    return density, coverage


def validate(runs, density, np):
    """Score the curve on whole runs, against the flat constant it replaces."""
    import hardware
    flat = hardware.CAPACITY_WH / 100.0
    edges = np.concatenate([[0.0], np.cumsum(density * BAND_WIDTH_PCT)])

    def E(pct):
        pct = min(max(pct, 0.0), 100.0)
        b = min(int(pct / BAND_WIDTH_PCT), NBANDS - 1)
        return edges[b] + density[b] * (pct - b * BAND_WIDTH_PCT)

    print("\nrun start           SoC span      measured    curve         %.0f Wh/pct" % flat)
    curveErr, flatErr = [], []
    for run in runs:
        wh = sum(w * (t1 - t0).total_seconds() / 3600.0 for (t0, w, _), t1 in run)
        hi, lo = run[0][0][2], run[-1][0][2]
        if hi - lo < 8.0 or (run[-1][1] - run[0][0][0]).total_seconds() < 1800:
            continue
        pc, pk = E(hi) - E(lo), (hi - lo) * flat
        curveErr.append((pc - wh) / wh * 100.0)
        flatErr.append((pk - wh) / wh * 100.0)
        print("  %s  %5.1f->%5.1f  %8.0f  %8.0f (%+5.1f%%)  %8.0f (%+6.1f%%)"
              % (run[0][0][0].strftime("%m-%d %H:%M"), hi, lo, wh, pc, curveErr[-1], pk, flatErr[-1]))
    if curveErr:
        print("\n  curve    : mean absolute error %.1f%% over %d runs"
              % (sum(abs(e) for e in curveErr) / len(curveErr), len(curveErr)))
        print("  constant : mean absolute error %.1f%%"
              % (sum(abs(e) for e in flatErr) / len(flatErr)))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=30, help="how far back to read (default 30)")
    ap.add_argument("--smoothing", type=float, default=4.0,
                    help="second-difference weight; the fit is insensitive between 0.2 and 10")
    ap.add_argument("--validate", action="store_true", help="score the result against whole runs")
    args = ap.parse_args()

    try:
        import numpy as np
        from scipy.optimize import nnls
    except ImportError:
        sys.exit("This script needs numpy and scipy:  pip install numpy scipy")

    samples = loadSamples(args.days)
    if not samples:
        sys.exit("No power_readings in the last %d days - is INFLUX_TOKEN set?" % args.days)
    runs = dischargeRuns(samples)
    A, y = buildEquations(runs, np)
    if not len(y):
        sys.exit("No usable discharge runs in the last %d days." % args.days)
    density, coverage = fit(A, y, np, nnls, args.smoothing)

    print("%d equations from %d discharge runs over %d days"
          % (len(y), len(runs), args.days))
    print("\n# Wh per SoC point, lowest band first, %g points per band." % BAND_WIDTH_PCT)
    print("WH_PER_PCT = [")
    for b in range(NBANDS):
        note = "  <- no coverage, this is the prior" if coverage[b] < 500 else ""
        print("    %6.1f,   # %3d-%3d%%%s" % (density[b], b * BAND_WIDTH_PCT, (b + 1) * BAND_WIDTH_PCT, note))
    print("]")
    print("\n# 0-100%% under this curve: %.0f Wh" % (density.sum() * BAND_WIDTH_PCT))

    if args.validate:
        validate(runs, density, np)


if __name__ == "__main__":
    main()
