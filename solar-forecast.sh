#!/bin/bash
# Print today's forecast PV generation (Wh), hour by hour, from a fresh plan.
#
#   ./solar-forecast.sh
#
# Runs plan-now.sh first so the forecast is current, then sums the pvD+pvI
# columns of the resulting plan file for today's date, grouped by hour. Those
# columns are already Wh per 15-minute interval (forecast.solar's hourly Wh
# / 4), so summing the 4 quarters in an hour gives that hour's total with no
# unit conversion.
set -u
cd "$(dirname "$0")"

./plan-now.sh
rc=$?
if [ "$rc" -ne 0 ]; then
  printf '%s\n' "plan-now.sh failed (exit $rc); no fresh forecast to summarise"
  exit $rc
fi

today=$(date +%Y%m%d)
hour=$(date +%H)
plan=plans/plan_${today}_${hour}.txt
d=$(date +%Y-%m-%d)

if [ ! -f "$plan" ]; then
  printf '%s\n' "ERROR: expected plan file $plan not found" >&2
  exit 1
fi

awk -v d="$d" '
  $1==d {
    hr=substr($2,1,2)
    hourly[hr] += $3+$4
    total += $3+$4
  }
  END {
    printf "PV forecast for %s, by hour:\n", d
    for (hr=0; hr<24; hr++) {
      key=sprintf("%02d",hr)
      if (key in hourly) printf "  %s:00   %5d Wh\n", key, hourly[key]
    }
    printf "  ------------------\n"
    printf "  total   %5d Wh (%.2f kWh)\n", total, total/1000
  }
' "$plan"
