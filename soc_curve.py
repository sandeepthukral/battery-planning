"""How many Wh sit behind each SoC percentage point - measured, not assumed.

hardware.CAPACITY_WH treats the gauge as linear: 1% is always CAPACITY_WH/100 Wh.
The AlphaESS gauge is not linear, and the error is not a constant offset that a
different capacity number could absorb. Measured against 20 days of
`power_readings` in the `alphaess` bucket, one SoC point is worth roughly:

    ~314 Wh from 95-100%    (the pack keeps taking charge after the gauge pins
                             at 100 - up to ~1.9 kWh on a sunny day - and gives
                             it back over the first few points on the way down)
    ~285-315 Wh from 35-95% (the working band, where the evening discharge lives)
    ~185-240 Wh from 20-35% (the gauge falls markedly faster than the energy does)

Planning at a flat 279 Wh/point therefore runs the modelled SoC down ~10% too
fast through the evening and ~20% too slow near the floor. Both show up on the
battery-plan dashboard as the planned line diverging from the actual one and
then crossing back under it before the reserve - see the alphaess-collector
investigation, 2026-08-06.

Derivation
----------
Each contiguous discharge run in InfluxDB supplies exact equations of the form

    E(soc_high) - E(soc_low) = integral of battery_power_w between the two

for every pair of gauge steps inside it: the integral is measured and both
endpoints are gauge readings, so no attribution assumption enters. ~100k such
equations from 470 runs were solved for the per-band Wh/point by non-negative
least squares, with a second-difference smoothness term and a coverage-weighted
prior. The prior is what fills the bands below the reserve floor, which the pack
never visits and which therefore carry no equations at all - those entries are
the prior, not a measurement, and are here only so the function stays total.

Checked against whole runs it was not fitted to reproduce individually: mean
absolute error 4.3% versus 11.5% for the flat constant, and without the flat
constant's systematic sign flip (it understates every long evening discharge by
7-14% and overstates every low-SoC one by 15-25%).

Rebuild it from newer data with scripts/fit_soc_curve.py.

Caveats worth keeping in view
----------------------------
- The curve is fitted to discharges at 3-5 kW, which is what the planner
  schedules. The gauge sags under load, so the same pack read slowly would not
  give the same numbers, most visibly in the 20-35% band.
- The 95-100% band is an average over days that were topped off and days that
  were not. A pack that never reached 100% holds less there than this says.
- Below 10% is prior, not data. The planner's reserve floor keeps it unused.
"""

# Wh per SoC point, lowest band first, 5 points per band.
BAND_WIDTH_PCT = 5.0
WH_PER_PCT = [
    314.2,   #   0-  5%  \ below the reserve floor: prior, never observed
    326.5,   #   5- 10%  /
    325.2,   #  10- 15%
    269.6,   #  15- 20%
    188.5,   #  20- 25%
    183.6,   #  25- 30%
    244.7,   #  30- 35%
    331.7,   #  35- 40%
    424.3,   #  40- 45%
    355.1,   #  45- 50%
    290.3,   #  50- 55%
    283.4,   #  55- 60%
    302.9,   #  60- 65%
    297.0,   #  65- 70%
    285.3,   #  70- 75%
    288.3,   #  75- 80%
    313.1,   #  80- 85%
    296.5,   #  85- 90%
    290.5,   #  90- 95%
    313.4,   #  95-100%
]

# Cumulative Wh at the bottom of each band, so a lookup is one add and one multiply.
_CUM_WH = [0.0]
for _d in WH_PER_PCT:
    _CUM_WH.append(_CUM_WH[-1] + _d * BAND_WIDTH_PCT)

CAPACITY_WH = _CUM_WH[-1]      # 0-100% under this curve, for callers that need one number


def wh_at_pct(pct):
    """Wh stored at a given gauge reading. Inverse of pct_at_wh()."""
    pct = min(max(float(pct), 0.0), 100.0)
    band = min(int(pct / BAND_WIDTH_PCT), len(WH_PER_PCT) - 1)
    return _CUM_WH[band] + WH_PER_PCT[band] * (pct - band * BAND_WIDTH_PCT)


def pct_at_wh(wh):
    """Gauge reading for a given stored energy. Inverse of wh_at_pct()."""
    wh = min(max(float(wh), 0.0), CAPACITY_WH)
    for band in range(len(WH_PER_PCT)):
        if wh <= _CUM_WH[band + 1]:
            return band * BAND_WIDTH_PCT + (wh - _CUM_WH[band]) / WH_PER_PCT[band]
    return 100.0
