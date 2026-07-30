#!/bin/zsh
# Make one advisory plan for right now, and print it as instructions.
#
# This is the LIVE path. It is not run-matrix.sh, which is a backtest harness over a fixed
# past year. Here every input is current: the SOC the battery actually holds, today's and
# (after ~13:00) tomorrow's day-ahead prices, and a fresh PV forecast.
#
#   ./plan-now.sh            plan from the current hour
#
# Advice only. Nothing is sent to the battery.
set -u
cd "$(dirname "$0")"
PY=.venv/bin/python

# Where to reach InfluxDB. Two environments, and the difference is not cosmetic:
#
#   container on the NAS : INFLUX_URL=http://influxdb:8086 comes from docker-compose. The
#                          service name resolves on the shared docker network; the LAN IP
#                          would mean hairpinning out of the container and back into the
#                          same box, which is at best fragile and at worst blocked.
#   laptop on the LAN    : nothing is set, so fall back to the NAS address below.
#
# Only default INFLUX_HOST when INFLUX_URL is absent. influx_source.config() prefers URL
# over HOST anyway, so setting both is merely confusing today - but it is the kind of
# confusion that survives until someone reorders those two lines.
if [ -z "${INFLUX_URL:-}" ]; then
  export INFLUX_HOST=${INFLUX_HOST:-192.168.68.105}
fi

today=$(date +%Y%m%d)
tomorrow=$(date -v+1d +%Y%m%d)
hour=$(date +%H)
year=$(date +%Y)

# Energy tax is set per calendar year by the government, VAT included.
case $year in
  2025) etax=0.12286 ;;
  2026) etax=0.11085 ;;
  *)    etax=${BT_ETAX:-0.11085}
        print "WARNING: no energy tax on file for $year; using $etax. Check the 2027 rate." ;;
esac

mkdir -p plans logs
log=logs/plan_${today}_${hour}.log

# BT_END=tomorrow, not today: outputOptimisationResult() truncates at 15:00 of the next day
# when it thinks another day is coming, which is a backtest chaining convention and cuts a
# live plan short. Giving it tomorrow takes the "write everything" branch.
#
# No -h: quarter-hour planning. NL day-ahead went to 15-minute MTU on 2025-10-01, and
# averaging to hours erases the short spikes that carry most of the winter arbitrage
# (measured +10-14% every winter month).
#
# BT_SALDERING is left at "auto": the planner derives it per interval from the date, so the
# regime changes by itself on 2027-01-01 with no edit here.
#
# Hardware (capacity, charge/discharge ceilings, cycle cost, grid connection limit) is NOT
# set here. It lives in one constant block at the top of Marstek-planning.py, so the live
# plan, the backtest and an interactive run cannot disagree about what the system is. That
# block is also where the 10 kW inverter / three-phase upgrade gets made. BT_CAP, BT_MAXCHG,
# BT_MAXDIS, BT_CYCLECOSTS and BT_GRIDMAX still override here for a one-off what-if:
#   BT_GRIDMAX=5750 ./plan-now.sh     # what the 3x25A single-phase connection would do
#
# stdin from /dev/null is load-bearing, not tidiness. Any variable not set above falls back
# to the constants in Marstek-planning.py, and _ask() only takes that path when there is no
# terminal. Run from a shell, stdout goes to the log but stdin is still the terminal, so it
# would prompt into the log file and wait forever for an answer nobody can see.
BT_START=$today BT_END=$tomorrow BT_STARTHOUR=$hour \
BT_INITCHARGE=influx BT_MINSOC=10 BT_RTE=90 \
BT_ETAX=$etax \
BT_XMLAVAIL=N BT_OVERWRITE=Y BT_PRICE_CACHE=price_cache \
  $PY Marstek-planning.py -s -p -u -b < /dev/null >> $log 2>&1
rc=$?

if [[ $rc -ne 0 ]]; then
  print "planner failed (exit $rc). Last lines of $log:"
  tail -20 $log
  exit $rc
fi

plan=plans/plan_${today}_${hour}.txt
mv entsoe-output${today}.txt $plan
print "plan written to $plan"

# We run every 3 hours; a plan shorter than that plus slack leaves gaps where nothing has
# been decided if the next run is late or fails. 12h is the floor: normally the horizon runs
# to ~15:00 next day (see the comment above BT_END), so hitting this means something starved
# the price fetch (e.g. a stale price_cache entry from before tomorrow's auction published).
#
# Capture first, print second, rather than piping into tee. A pipeline would make $? the
# exit status of tee (always 0) and hide the check entirely, and the usual cure - reading
# the pipeline's first element - is not portable:
#
#   zsh   ${pipestatus[1]}     arrays are 1-indexed
#   bash  ${PIPESTATUS[0]}     arrays are 0-indexed
#
# Same concept, same spelling apart from case, different index. Translating this file to
# bash by mechanically lowercasing would read tee's status and leave the guard compiled,
# tested and dead. Detecting the shell (`if [ -n "$ZSH_VERSION" ]`) works, but it keeps a
# silent-failure mode alive to solve a problem that disappears if the pipe does. advise.py
# prints a few lines instantly, so there is nothing to stream.
advise_out=$($PY advise.py --min-hours 12 "$plan" 2>&1)
advise_rc=$?
printf '%s\n' "$advise_out" | tee -a "$log"
if [ "$advise_rc" -ne 0 ]; then
  printf '%s\n' "plan horizon check failed (see ERROR above); treating this run as failed"
  exit 1
fi
