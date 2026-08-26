#!/bin/sh
# Yesterday's plans, scored against what actually happened. For DSM Task Scheduler, run as
# root, daily. Advice only - this reads history and writes a score; nothing is sent anywhere.
#
# Sibling of plan.sh, and deliberately a separate task rather than a tail on the planning run:
# a planning failure must not take the report with it, and vice versa.
#
# Timing: 06:10. Not just after midnight - the report needs the whole day's actuals, and the
# plan it scores is the last run that covered the day's first interval, written just before
# midnight. A 00:05 run would be racing both. By 06:10 yesterday is closed on both sides: the
# committed plan exists, and the collector has been writing actuals continuously since.
#
# 06:10 also sits in the quietest part of the planning schedule - an hour after the 05:05 run,
# two before the 08:05 one - so the two jobs never contend. An earlier draft chose 08:10 for
# landing "just after the 08:05 planning run", which had it backwards: that is the slot most
# likely to overlap, not least. The report should be finished before the user is awake.
set -eu

# DSM Task Scheduler runs with a minimal PATH; docker lives in /usr/local/bin.
PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
export PATH

# Overridable so this script can be exercised by a test without the NAS path existing
# (see tests/test_report_sh.py); the DSM task never sets REPO_DIR, so it always gets
# the real path.
REPO_DIR="${REPO_DIR:-/volume1/docker/battery-planning}"
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

# Keeps the newest 7 reports directly under data/reports (glanceable from the viewer's
# root listing) and files everything older into data/reports/YYYY/MM/, derived from the
# report_YYYYMMDD.txt filename rather than the run date - a backfilled or rerun day sorts
# into the month it is about, not the month it happened to be generated in.
#
# Filenames are zero-padded YYYYMMDD, so lexicographic sort is chronological sort - no
# date parsing needed to order them, only to build the destination path.
#
# Run twice per invocation: once before writing (tidies any backlog, e.g. the first run
# after this feature shipped, and picks up files a previous run left behind), once after
# (files today's report the moment it pushes the root past 7).
archive_old_reports() {
    dir="data/reports"
    keep=7
    files=$(find "$dir" -maxdepth 1 -type f -name 'report_????????.txt' | sort)
    total=$(printf '%s\n' "$files" | grep -c 'report_' || true)
    if [ "$total" -le "$keep" ]; then
        return 0
    fi
    move_count=$((total - keep))
    printf '%s\n' "$files" | head -n "$move_count" | while IFS= read -r f; do
        base=$(basename "$f")
        datepart=${base#report_}
        datepart=${datepart%.txt}
        destdir="$dir/$(echo "$datepart" | cut -c1-4)/$(echo "$datepart" | cut -c5-6)"
        mkdir -p "$destdir"
        mv "$f" "$destdir/"
    done
}
archive_old_reports

# A rerun of an already-archived day (e.g. `report.sh 2026-01-15` months later) would
# otherwise leave a stale duplicate sitting in the archive while the fresh one lands in
# root - drop the stale copy so there is only ever one copy of a given day's report.
find data/reports -mindepth 2 -type f -name "report_$(echo "$DAY" | tr -d -).txt" -delete

# --write stores the per-interval comparison as measurement plan_score, so the Grafana panel
# queries the same numbers this text file shows instead of recomputing them in Flux and
# disagreeing. The text file is the record a person reads.
#
# --no-deps because alphaess-net is external and its containers belong to the collector stack.
# `|| rc=$?` rather than a bare call: set -e would otherwise exit before the status could be
# reported, and the task log would end without saying what happened.
OUT="data/reports/report_$(echo "$DAY" | tr -d -).txt"

rc=0
# ${DOCKER:-docker}, not a bare `docker`: lets tests/test_report_sh.py substitute a
# stub without fighting the PATH rewrite two lines above, which would otherwise put
# the real /usr/local/bin/docker ahead of anything a test prepends to PATH.
"${DOCKER:-docker}" compose run --rm --no-deps planner \
    python3 /app/report_day.py "$DAY" --write > "$OUT.$$" 2>&1 || rc=$?

# Move into place only once the run is over, so a half-written report is never mistaken for
# a finished one - and so re-running a day leaves the previous file intact until the new one
# is complete.
#
# Only on rc 0 or 1, and deliberately so. rc 1 means "no plans stored for that day" -
# report_day.py still finished and wrote something worth reading. Any other rc is a
# real failure (the docker run itself, an InfluxDB error, a traceback), and OUT.$$ at
# that point holds that failure's output, not a report - moving it into place would
# silently replace yesterday's good report with today's error message. Left as OUT.$$
# instead, so the next successful run overwrites it in the ordinary course of things.
#
# rc 3 belongs in that second group on purpose: report_day.py ran fine, and its section 4
# found that the plan it scored does not conserve energy. That output is a diagnosis, not
# a report, and publishing it would put a number nobody can earn in front of a reader -
# which is exactly what happened, unnoticed, for months. Yesterday's report stays put.
if [ "$rc" -eq 0 ] || [ "$rc" -eq 1 ]; then
    mv "$OUT.$$" "$OUT"
    archive_old_reports
    cat "$OUT"
    echo "$(stamp) report: done (exit $rc) -> $OUT"
else
    echo "$(stamp) report: FAILED (exit $rc) - leaving the previous $OUT untouched"
    echo "$(stamp) report: failure output kept at $OUT.$$"
    cat "$OUT.$$"
fi
exit $rc
