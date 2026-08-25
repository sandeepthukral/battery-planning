#!/bin/sh
# One planning pass, for DSM Task Scheduler. Run as root, hourly at :55 - five minutes BEFORE
# the hour it plans for, not after it. The 13:55 run is the first to see tomorrow's day-ahead
# prices, published around 13:00.
#
# The :55 is deliberate and was arrived at by observation. On the old :05 schedule the battery
# sat idle for roughly the first ten minutes after each run; publishing the plan just before an
# interval boundary means it is already in force when the new interval starts, which removed
# the stall (or at least halved the window it can occupy). The cause was never diagnosed, so
# treat this as a mitigation: if the stall reappears, note whether it is charging or
# discharging that stops, which should identify the path.
#
# Advice only. Nothing here sends anything to the battery.
#
# Modelled on alphaess-collector/scripts/daily-savings.sh, which runs on the same NAS.
set -eu

# DSM Task Scheduler runs with a minimal PATH; make sure docker is findable.
PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
export PATH

REPO_DIR="/volume1/docker/battery-planning"
LOCK_DIR="$REPO_DIR/data/.plan.lock"
STALE_MINUTES=20

cd "$REPO_DIR"

stamp() { date '+%Y-%m-%d %H:%M:%S'; }

# Don't let a slow run collide with the next firing. A stuck run holding the lock forever
# would silently stop all planning, so a lock older than 20 minutes is treated as abandoned -
# a plan takes a minute or two, and the schedule fires hourly, so 20 minutes is well clear of
# a healthy run and well short of the next one. (At the old 3-hourly cadence this was 60,
# a third of the 180-minute gap; 20 keeps the same ratio against the new 60-minute gap.)
if [ -d "$LOCK_DIR" ] && [ -z "$(find "$LOCK_DIR" -maxdepth 0 -mmin +$STALE_MINUTES 2>/dev/null)" ]; then
    echo "$(stamp) plan: another run still holds $LOCK_DIR - skipping this one"
    exit 0
fi
rm -rf "$LOCK_DIR"

# mkdir is atomic, so two firings cannot both believe they won. flock would be tidier but is
# not guaranteed present on DSM.
mkdir -p "$(dirname "$LOCK_DIR")"
mkdir "$LOCK_DIR"
trap 'rm -rf "$LOCK_DIR"' EXIT INT TERM

echo "$(stamp) plan: starting"

# --no-deps because alphaess-net is external and its containers are owned by the collector
# stack; this must never try to start or stop them.
#
# `|| rc=$?` rather than a bare call: `set -e` would otherwise exit on a failing run before
# the status could be captured, and the task log would end without saying what happened.
rc=0
docker compose run --rm --no-deps planner || rc=$?

# Record what the weather forecast said at this moment. Deliberately AFTER the plan and
# deliberately non-fatal: nothing in the planner consumes this data, and nothing will until a
# temperature term in the load forecast is actually justified, so a weather outage must never
# delay or fail a plan. Its own exit code is reported and then dropped.
#
# It rides this schedule rather than getting one of its own because the point of the series is
# to be contemporaneous with the plan it would have informed - a forecast captured at some
# unrelated hour cannot be lined up against a plan run afterwards.
wrc=0
docker compose run --rm --no-deps planner python3 /app/capture_weather.py || wrc=$?
[ "$wrc" -eq 0 ] || echo "$(stamp) plan: weather capture failed (exit $wrc) - plan unaffected"

echo "$(stamp) plan: done (exit $rc)"
exit $rc
