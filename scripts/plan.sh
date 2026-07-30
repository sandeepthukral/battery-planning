#!/bin/sh
# One planning pass, for DSM Task Scheduler. Run as root, every 3 hours from 02:05, giving
# 02/05/08/11/14/17/20/23. The 14:05 run is the first to see tomorrow's day-ahead prices,
# published around 13:00.
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
STALE_MINUTES=60

cd "$REPO_DIR"

stamp() { date '+%Y-%m-%d %H:%M:%S'; }

# Don't let a slow run collide with the next firing. A stuck run holding the lock forever
# would silently stop all planning, so a lock older than an hour is treated as abandoned -
# a plan takes a minute or two, and the schedule fires every three hours, so an hour is well
# clear of a healthy run and well short of the next one.
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

echo "$(stamp) plan: done (exit $rc)"
exit $rc
