#!/bin/sh
# Yesterday's plans, scored against what actually happened. For DSM Task Scheduler, run as
# root, daily. Advice only - this reads history and writes a score; nothing is sent anywhere.
#
# Sibling of plan.sh, and deliberately a separate task rather than a tail on the planning run:
# a planning failure must not take the report with it, and vice versa.
#
# Timing. Run it at 08:10, not just after midnight. The report scores each interval against
# the plan in force for it, and the last plan of a day is written at 23:05 - so a run at 00:05
# would be racing the day it is trying to score. 08:10 also lands after the 08:05 planning
# run, and the lock below keeps them from overlapping if that one runs long.
set -eu

# DSM Task Scheduler runs with a minimal PATH; docker lives in /usr/local/bin.
PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
export PATH

REPO_DIR="/volume1/docker/battery-planning"
LOCK_DIR="$REPO_DIR/data/.report.lock"
STALE_MINUTES=30

cd "$REPO_DIR"

stamp() { date '+%Y-%m-%d %H:%M:%S'; }

# Its own lock, not plan.sh's: the two do different work and either may legitimately run while
# the other does. A lock older than half an hour is treated as abandoned - the report is a
# handful of queries and finishes in seconds, so anything near that is already wedged.
if [ -d "$LOCK_DIR" ] && [ -z "$(find "$LOCK_DIR" -maxdepth 0 -mmin +$STALE_MINUTES 2>/dev/null)" ]; then
    echo "$(stamp) report: another run still holds $LOCK_DIR - skipping this one"
    exit 0
fi
rm -rf "$LOCK_DIR"

mkdir -p "$(dirname "$LOCK_DIR")"
mkdir "$LOCK_DIR"
trap 'rm -rf "$LOCK_DIR"' EXIT INT TERM

# report_day.py defaults to yesterday on its own, but the date is worked out here and passed
# in explicitly so the output filename and the report cannot disagree about which day they
# describe. An argument re-runs a specific day without editing the task:
#
#     sudo /volume1/docker/battery-planning/scripts/report.sh 2026-07-31
#
# TZ matters: "yesterday" at 08:10 local is a different date from "yesterday" in UTC for two
# hours of every summer night, and the container resolves dates in Europe/Amsterdam.
TZ="${BT_TZ:-Europe/Amsterdam}"
export TZ
DAY="${1:-$(date -d yesterday '+%Y-%m-%d')}"

echo "$(stamp) report: starting for $DAY"

mkdir -p data/reports

# --write stores the per-interval comparison as measurement plan_score, so the Grafana panel
# queries the same numbers this text file shows instead of recomputing them in Flux and
# disagreeing. The text file is the record a person reads.
#
# --no-deps because alphaess-net is external and its containers belong to the collector stack.
# `|| rc=$?` rather than a bare call: set -e would otherwise exit before the status could be
# reported, and the task log would end without saying what happened.
OUT="data/reports/report_$(echo "$DAY" | tr -d -).txt"

rc=0
docker compose run --rm --no-deps planner \
    python3 /app/report_day.py "$DAY" --write > "$OUT.$$" 2>&1 || rc=$?

# Move into place only once the run is over, so a half-written report is never mistaken for
# a finished one - and so re-running a day leaves the previous file intact until the new one
# is complete.
mv "$OUT.$$" "$OUT"

cat "$OUT"

# Exit 1 means "no plans stored for that day" - a day the planner did not run, which is worth
# seeing in the task log rather than swallowing.
echo "$(stamp) report: done (exit $rc) -> $OUT"
exit $rc
