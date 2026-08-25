#!/bin/bash
# Make one advisory plan for right now, and print it as instructions.
#
# This is the LIVE path. It is not run-matrix.sh, which is a backtest harness over a fixed
# past year. Here every input is current: the SOC the battery actually holds, today's and
# (after ~13:00) tomorrow's day-ahead prices, and a fresh PV forecast.
#
#   ./plan-now.sh            plan from the current hour
#
# Advice only. Nothing is sent to the battery.
#
# bash, not zsh: this runs both on a Mac and in a python:3.12-slim container, which has no
# zsh. Everything below is deliberately written to work on both, because the whole point of
# a scheduled planner is that nobody is watching it when it breaks.
set -u

# Where the code lives vs. where output goes. On the Mac these are the same directory and
# nothing changes. In the container the code is baked into the image at /app and every
# written artefact - plans/, logs/, price_cache/, pv_cache/, the planner's own
# entsoe-output file - belongs on the bind-mounted /data, which survives a rebuild.
# BT_DATA_DIR is what splits them. Everything the planner writes is CWD-relative, so
# changing directory is the whole mechanism; the scripts are then called by absolute path.
scriptDir=$(cd "$(dirname "$0")" && pwd)
cd "${BT_DATA_DIR:-$scriptDir}"

# advise.py imports influx_source without the sys.path guard that Marstek-planning.py has,
# so once the CWD is no longer the code directory it needs telling where to look. The
# container also sets this, harmlessly; here it makes a Mac run with BT_DATA_DIR work too.
export PYTHONPATH=${PYTHONPATH:-$scriptDir}

# Pin the wall clock before reading it. Everything here - the plan filename, BT_START,
# BT_STARTHOUR, the energy-tax year - comes from `date`, which follows TZ. A container
# defaults to UTC, so without this the 13:55 run would ask the planner to start at hour 11
# and would write its plan under the wrong hour, while Marstek-planning.py's own clock (see
# its "Wall clock" block) correctly said 13. The two must not be allowed to disagree.
#
# Keyed off BT_TZ, not TZ, and deliberately so. Defaulting with ${TZ:-...} would let an
# image that sets TZ=UTC win, which is precisely the case this exists to defend against.
# BT_TZ is the single knob: Marstek-planning.py reads the same variable for its own clock,
# so the shell and the planner cannot end up in different timezones.
#   TZ=UTC ./plan-now.sh        still plans in Amsterdam time
#   BT_TZ=UTC ./plan-now.sh     plans in UTC, both halves agreeing
export BT_TZ=${BT_TZ:-Europe/Amsterdam}
export TZ=$BT_TZ

# The venv is a darwin build and is gitignored, so it exists on the Mac and never in the
# container, where dependencies are installed into the image instead. Prefer it when present.
if [ -z "${PY:-}" ]; then
  if [ -x "$scriptDir/.venv/bin/python" ]; then PY="$scriptDir/.venv/bin/python"; else PY=python3; fi
fi

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

# "Tomorrow" has no portable spelling: GNU date wants -d, BSD/macOS date wants -v, and each
# rejects the other's flag. Try GNU first, fall back to BSD. Note this is NOT the change the
# deployment plan originally prescribed - switching outright to "date -d tomorrow" fixes
# Linux and breaks the Mac, where date exits 1 with "illegal option -- d".
tomorrow=$(date -d tomorrow +%Y%m%d 2>/dev/null || date -v+1d +%Y%m%d)
if [ -z "$tomorrow" ]; then
  # Neither form worked. Say so and stop: an empty BT_END reaches _ask(), which warns and
  # falls back to a default end date, quietly producing a plan over the wrong window.
  printf '%s\n' "ERROR: could not compute tomorrow's date with either 'date -d' or 'date -v'" >&2
  exit 1
fi
today=$(date +%Y%m%d)
hour=$(date +%H)
year=$(date +%Y)

# Energy tax is set per calendar year by the government, VAT included.
case $year in
  2025) etax=0.12286 ;;
  2026) etax=0.11085 ;;
  *)    etax=${BT_ETAX:-0.11085}
        printf '%s\n' "WARNING: no energy tax on file for $year; using $etax. Check the 2027 rate." ;;
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
  $PY "$scriptDir/Marstek-planning.py" -s -p -u -b < /dev/null >> "$log" 2>&1
rc=$?

if [ "$rc" -ne 0 ]; then
  printf '%s\n' "planner failed (exit $rc). Last lines of $log:"
  tail -20 $log
  exit $rc
fi

plan=plans/plan_${today}_${hour}.txt
planOutput=entsoe-output${today}.txt
if [ ! -f "$planOutput" ]; then
  # The planner exited 0 above but did not write the file this expects under
  # today's date. The known cause is the midnight race between this script's own
  # `date` and Marstek-planning.py's separate clock (see its "Wall clock" block and
  # the BT_INITCHARGE=influx guard, CODE-REVIEW.md C7): the planner refuses in that
  # case, which should have been caught by `rc -ne 0` above, but a mismatch that
  # instead sent it down the HISTORICAL branch writes its output under a DIFFERENT
  # date's filename rather than failing at all. Naming this here turns a bare `mv:
  # no such file` followed by a confusing advise.py traceback into one sentence
  # that says what actually happened.
  printf '%s\n' "ERROR: expected planner output $planOutput not found. Last lines of $log:" >&2
  tail -20 "$log" >&2
  exit 1
fi
mv "$planOutput" "$plan"
printf '%s\n' "plan written to $plan"

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
advise_out=$($PY "$scriptDir/advise.py" --min-hours 12 "$plan" 2>&1)
advise_rc=$?
printf '%s\n' "$advise_out" | tee -a "$log"
if [ "$advise_rc" -ne 0 ]; then
  printf '%s\n' "plan horizon check failed (see ERROR above); treating this run as failed"
  exit 1
fi
