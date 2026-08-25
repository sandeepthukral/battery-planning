"""The one number that has to agree across every entry point: the battery's rated
capacity in Wh.

CODE-REVIEW.md D4. Before this, 27900 was written down three times - once in
planner.py (which plans against it), once in advise.py (which prints SoC
percentages against it) and once in report_day.py (same, for the day-after report).
They agreed only because three people happened to type the same number; a future
capacity change (the planned 27,900 -> ~30,500 Wh upgrade - see TODO.md, "parked, not
scheduled") would need to be made in three places, and any missed one prints a wrong
percentage silently rather than failing.

Only capacity lives here. maxChargeSpeed, maxDischargeSpeed, gridConnectionLimit and
cycleCosts are not duplicated anywhere else, so there is no drift risk to fix for them
- their measured values and the reasoning behind them stay in planner.py's
"Battery and inverter" comment block, which is the right place for a narrative that
long.
"""

CAPACITY_WH = 27900      # Wh, AlphaESS - see planner.py for the measured
                          # charge/discharge ceilings and the upgrade path
