#!/bin/bash
# Start as root, work out who owns the data mount, become that user, run the planner.
#
# The alternative - baking a UID into the image at build time and asking whoever deploys it
# to match the host - fails silently when it is wrong: the planner's cache writes are wrapped
# in try/except, so an unwritable mount just means every run refetches, and the first visible
# symptom is forecast.solar rate-limiting hours later. Detecting the owner removes the
# question, and the write probe below turns whatever is left into an error at startup.
set -euo pipefail

dataDir=${BT_DATA_DIR:-/data}

if [ "$(id -u)" -ne 0 ]; then
    # Already unprivileged: compose set `user:`, or this was run with --user. There is nothing
    # to detect and no way to chown, so skip straight to the probe, which still reports the
    # truth about this mount.
    uid=$(id -u); gid=$(id -g); asUser=()
else
    mkdir -p "$dataDir"
    uid=$(stat -c %u "$dataDir")
    gid=$(stat -c %g "$dataDir")

    if [ "$uid" -eq 0 ]; then
        # Root owns the mount, which means docker created ./data itself because it did not
        # exist - `sudo docker compose` does exactly this. No host user has a claim on it yet,
        # so take it for an unprivileged user rather than running the planner as root.
        uid=${PLANNER_UID:-1000}
        gid=${PLANNER_GID:-$uid}
        chown "$uid:$gid" "$dataDir"
        printf 'entrypoint: %s was root-owned (docker created it); now %s:%s\n' \
               "$dataDir" "$uid" "$gid"
    fi

    # Give the UID a name. Nothing here needs a home directory, but a UID with no passwd entry
    # makes getpwuid() raise, which surfaces from unrelated library code as a confusing
    # KeyError. Cheaper to add the line than to debug that later.
    if ! getent group "$gid" >/dev/null; then
        printf 'planner:x:%s:\n' "$gid" >> /etc/group
    fi
    if ! getent passwd "$uid" >/dev/null; then
        printf 'planner:x:%s:%s:planner:/tmp:/usr/sbin/nologin\n' "$uid" "$gid" >> /etc/passwd
    fi

    asUser=(gosu "$uid:$gid")
fi

# Actually write a file rather than testing the permission bits. Synology shares carry DSM
# ACLs on top of the POSIX mode, so `test -w` can say yes where a write still fails - and this
# check exists precisely to catch the case the mode bits do not describe.
probe=$dataDir/.write-probe.$$
if ! ${asUser[@]+"${asUser[@]}"} sh -c "touch '$probe' && rm -f '$probe'" 2>/dev/null; then
    cat >&2 <<EOF
ERROR: $dataDir is not writable by uid $uid (gid $gid).

  The planner keeps its plans, logs and its price and PV caches there. Without it, every run
  refetches from forecast.solar, whose free tier allows about 12 requests an hour, and the
  run eventually fails looking like an API problem.

  On the host, the mounted directory needs to be writable by that uid:

      sudo chown -R $uid:$gid /volume1/docker/battery-planning/data

  Refusing to start rather than running and losing the cache silently.
EOF
    exit 1
fi

# A token has to be present. This used to be `${INFLUX_TOKEN:?...}` in docker-compose, but
# there are now two acceptable names - the collector issues INFLUX_TOKEN_PLANNING, scoped to
# read:alphaess + write:planning - and compose interpolation cannot say "one of these two".
# Checking here keeps the fail-fast and gains the ability to accept either.
#
# Only the environment is checked. A token in .env beside influx_source.py would also work,
# but that file does not exist in the image; in a container the value arrives through
# docker-compose or not at all, so an empty one here means the run is already lost.
if [ -z "${INFLUX_TOKEN:-}" ] && [ -z "${INFLUX_TOKEN_PLANNING:-}" ]; then
    cat >&2 <<'EOF'
ERROR: no InfluxDB token in the environment.

  The planner builds every plan from the measured state of charge and refuses to run
  without it, so this stops here rather than three minutes in.

  Set one of these in the .env beside docker-compose.yml - either name is read, and the
  more specific one wins if both appear:

      INFLUX_TOKEN_PLANNING=...    the scoped token from alphaess-collector
      INFLUX_TOKEN=...             the same value under the generic name

EOF
    exit 1
fi

# HOME is not set by gosu. Point it somewhere writable so anything calling expanduser("~")
# lands in the container's own tmpfs instead of failing or writing into the mount.
export HOME=/tmp

exec ${asUser[@]+"${asUser[@]}"} "$@"
