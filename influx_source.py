"""InfluxDB data source for Marstek-planning.py, backed by alphaess-collector.

Replaces the Domoticz feeds that had no standalone substitute:

  * battery state of charge          -> soc_percent      (was Domoticz device 372)
  * recent hourly house load         -> load_power_w     (was Domoticz device 178)
  * recent hourly PV generation      -> pv_power_w       (was Domoticz device 3)

The collector stores INSTANTANEOUS POWER in W, sampled every POLL_INTERVAL_SECONDS
(30s by default), in measurement "power_readings" of bucket "alphaess", tagged by
sys_sn. Hourly energy is therefore the mean power over the hour, in Wh:

    Wh = mean(W) over the hour x 1 h

Each hour is returned with a coverage figure (samples seen / samples expected) so a
partially-collected hour can be rejected instead of silently reading as a low hour -
the failure mode that matters here, since a collector outage looks exactly like a
quiet house.

Timestamps: the planner works in Europe/Amsterdam local time. Hourly windows are
aggregated in UTC and relabelled locally, which is exact because every Amsterdam
offset is a whole number of hours.

Configuration is resolved in three steps, first hit wins:

    1. the real environment          (docker-compose, plan-now.sh, an export)
    2. this repo's own .env          (see .env.example; INFLUX_ENV_FILE overrides the path)
    3. the collector's .env          (../../alphaess-collector/.env, if it is a sibling)

Step 3 is a convenience for a Mac checkout sitting next to alphaess-collector, so the
token does not have to be copied by hand. It is a RELATIVE path from this file and
resolves to nothing once the code is at /app in a container - which is why step 2 exists
and is the one to use anywhere but a dev machine.

    INFLUX_URL       e.g. http://influxdb:8086        (required, or INFLUX_HOST)
    INFLUX_HOST      host only; INFLUX_PORT defaults to 8086
    INFLUX_TOKEN     API token                        (required)
    INFLUX_ORG       default "home"
    INFLUX_BUCKET    default "alphaess"
    ALPHAESS_SYS_SN  optional; filters to one system
    INFLUX_ENV_FILE  path to this repo's .env, default alongside this file

Self-test:

    python3 influx_source.py            # connectivity + last SoC + 7-day load profile
"""
import csv as _csv
import io
import os
from datetime import datetime, timedelta, timezone

import requests

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Europe/Amsterdam")
except ImportError:                                     # pragma: no cover
    LOCAL_TZ = None

MEASUREMENT = "power_readings"
FIELD_LOAD = "load_power_w"
FIELD_PV = "pv_power_w"
FIELD_SOC = "soc_percent"
FIELD_BATTERY = "battery_power_w"
FIELD_GRID = "grid_power_w"

_HERE = os.path.dirname(os.path.abspath(__file__))

# This repo's own .env: the primary place for connection settings, and the only one that
# travels. Gitignored - see .env.example for the keys.
DEFAULT_ENV_FILE = os.path.join(_HERE, ".env")

# Fallback for a dev checkout that sits beside alphaess-collector, so the token need not be
# copied. Relative to THIS FILE, so at /app in a container it resolves to a path that cannot
# exist; that is expected, and step 1 or 2 above is meant to have supplied the token by then.
COLLECTOR_ENV_FILE = os.path.join(_HERE, "..", "..", "alphaess-collector", ".env")

_config = None


def _read_env_file(path):
    """Parse a KEY=VALUE .env file. Missing file is not an error."""
    values = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                values[key.strip()] = val.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def config():
    """Resolve connection settings: real environment wins over the collector .env."""
    global _config
    if _config is None:
        env_file = os.environ.get("INFLUX_ENV_FILE", DEFAULT_ENV_FILE)
        fromfile = _read_env_file(env_file)
        collector = _read_env_file(COLLECTOR_ENV_FILE)

        def pick(key, default=""):
            # real environment, then this repo's .env, then the collector's. Empty values
            # fall through rather than winning, so a commented-out or blanked key in one
            # file does not shadow a real value in the next.
            return (os.environ.get(key) or fromfile.get(key)
                    or collector.get(key) or default)

        url = pick("INFLUX_URL")
        if not url:
            # the collector's .env records only the port, since it reaches InfluxDB
            # over the docker network; standalone we need a routable host
            host = pick("INFLUX_HOST")
            port = pick("INFLUX_PORT", "8086")
            url = "http://%s:%s" % (host, port) if host else ""
        _config = {
            "url": url.rstrip("/"),
            "token": pick("INFLUX_TOKEN"),
            "org": pick("INFLUX_ORG", "home"),
            "bucket": pick("INFLUX_BUCKET", "alphaess"),
            "sys_sn": pick("ALPHAESS_SYS_SN"),
            "poll_seconds": float(pick("POLL_INTERVAL_SECONDS", "30") or 30),
            "env_file": env_file,
            "collector_env_file": COLLECTOR_ENV_FILE,
        }
    return _config


def configured():
    c = config()
    return bool(c["url"] and c["token"])


def _query(flux):
    """POST a Flux query, return parsed rows as dicts. Raises on transport failure."""
    c = config()
    if not configured():
        # Name what is missing and every place that was searched. The old message mentioned
        # only INFLUX_ENV_FILE, which pointed at the wrong fix in a container: there the
        # answer is to pass INFLUX_TOKEN through docker-compose, not to repath a .env.
        missing = [k for k, v in (("INFLUX_URL (or INFLUX_HOST)", c["url"]),
                                  ("INFLUX_TOKEN", c["token"])) if not v]
        raise RuntimeError(
            "InfluxDB is not configured: missing %s.\n"
            "  Searched: the environment, then %s, then %s.\n"
            "  Copy .env.example to .env and fill it in, or set the variables directly "
            "(in a container, pass them from docker-compose)."
            % (" and ".join(missing), c["env_file"], c["collector_env_file"]))
    resp = requests.post(
        c["url"] + "/api/v2/query",
        params={"org": c["org"]},
        data=flux.encode("utf-8"),
        headers={"Authorization": "Token " + c["token"],
                 "Content-Type": "application/vnd.flux",
                 "Accept": "application/csv"},
        timeout=30)
    resp.raise_for_status()
    rows = []
    for row in _csv.DictReader(io.StringIO(resp.text)):
        if not row or row.get("_time") in (None, "") and row.get("_value") in (None, ""):
            continue
        if (row.get("result") or "").startswith("#"):
            continue
        rows.append(row)
    return rows


def _sys_filter():
    c = config()
    if not c["sys_sn"]:
        return ""
    return '\n  |> filter(fn: (r) => r.sys_sn == "%s")' % c["sys_sn"]


def _parse_time(value):
    """RFC3339 -> aware datetime."""
    v = value.replace("Z", "+00:00")
    return datetime.fromisoformat(v)


def latestSocPercent(within_minutes=30):
    """Most recent state of charge in percent, or None if nothing recent enough."""
    flux = '''from(bucket: "%s")
  |> range(start: -%dm)
  |> filter(fn: (r) => r._measurement == "%s" and r._field == "%s")%s
  |> last()''' % (config()["bucket"], int(within_minutes), MEASUREMENT, FIELD_SOC, _sys_filter())
    rows = _query(flux)
    if not rows:
        return None
    try:
        return float(rows[-1]["_value"])
    except (KeyError, ValueError):
        return None


def hourlyEnergyWh(field, start, stop, min_coverage=0.5, clamp_negative=False):
    """Hourly energy in Wh for a power field, keyed "YYYY-MM-DD HH" in local time.

    start/stop are aware datetimes. Hours whose sample coverage falls below
    min_coverage are omitted rather than reported as a low hour.

    clamp_negative floors samples at zero before averaging. Use it for load and PV,
    which cannot physically be negative but do occasionally report so during fast
    power swings (~0.07% of load samples). Never use it for battery_power_w or
    grid_power_w, where the sign carries the direction of flow.
    """
    c = config()
    rng = '|> range(start: %s, stop: %s)' % (
        start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        stop.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    base = 'from(bucket: "%s")\n  %s\n  |> filter(fn: (r) => r._measurement == "%s" and r._field == "%s")%s' % (
        c["bucket"], rng, MEASUREMENT, field, _sys_filter())
    if clamp_negative:
        base += '\n  |> map(fn: (r) => ({r with _value: if r._value < 0.0 then 0.0 else r._value}))'

    means = _query(base + '\n  |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)')
    counts = _query(base + '\n  |> aggregateWindow(every: 1h, fn: count, createEmpty: false)')

    expected = 3600.0 / c["poll_seconds"] if c["poll_seconds"] else 120.0
    seen = {}
    for row in counts:
        try:
            seen[row["_time"]] = float(row["_value"])
        except (KeyError, ValueError):
            continue

    out = {}
    for row in means:
        try:
            ts, watts = row["_time"], float(row["_value"])
        except (KeyError, ValueError):
            continue
        if seen.get(ts, 0.0) / expected < min_coverage:
            continue
        # aggregateWindow stamps a window with its END; the planner labels hours by
        # their START, so step back one hour before relabelling
        local = (_parse_time(ts) - timedelta(hours=1)).astimezone(LOCAL_TZ)
        out[local.strftime("%Y-%m-%d %H")] = watts      # mean W over 1h == Wh
    return out


def hourlyAvgProfileWh(field=FIELD_LOAD, days=7, weightIncrease=0.0):
    """Average energy per hour-of-day over the last `days`, as [["HH", Wh], ...].

    Matches the shape Marstek-planning.py's calcHourlyAvgUsage() returns. More
    recent days can be weighted up via weightIncrease (0 = flat average), mirroring
    the original Domoticz behaviour.
    """
    now = datetime.now(LOCAL_TZ) if LOCAL_TZ else datetime.now(timezone.utc)
    start = (now - timedelta(days=days)).replace(minute=0, second=0, microsecond=0)
    # load and PV cannot be negative; the sign-carrying fields must not be clamped
    hours = hourlyEnergyWh(field, start, now,
                           clamp_negative=field in (FIELD_LOAD, FIELD_PV))

    totals = [0.0] * 24
    weights = [0.0] * 24
    day_index = {}
    for key in sorted(hours):
        day, hh = key[:10], int(key[11:13])
        if day not in day_index:
            day_index[day] = len(day_index)
        w = 1.0 + weightIncrease * day_index[day]
        totals[hh] += hours[key] * w
        weights[hh] += w

    return [["%02d" % h, int(round(totals[h] / weights[h])) if weights[h] else 0]
            for h in range(24)], len(day_index)


def hourValueList(field, runDate, days=2, scale=1.0):
    """Hourly values shaped like getHrValueFromBIGDB(): [[seq, "YYYY-MM-DD", "HH", Wh]]."""
    start = runDate if runDate.tzinfo else runDate.replace(tzinfo=LOCAL_TZ)
    stop = start + timedelta(days=days)
    hours = hourlyEnergyWh(field, start, stop)
    out = []
    for seq, key in enumerate(sorted(hours), start=1):
        out.append([seq, key[:10], key[11:13], int(round(hours[key] * scale))])
    return out


def _selftest():
    c = config()
    print("InfluxDB configuration")
    print("  env file : %s (%s)" % (c["env_file"], "found" if os.path.exists(c["env_file"]) else "MISSING"))
    print("  url      : %s" % (c["url"] or "<not set>"))
    print("  org      : %s" % c["org"])
    print("  bucket   : %s" % c["bucket"])
    print("  sys_sn   : %s" % (c["sys_sn"] or "<all>"))
    print("  token    : %s" % ("set (%d chars)" % len(c["token"]) if c["token"] else "<not set>"))
    if not configured():
        print("\nNot configured. Set INFLUX_URL (or INFLUX_HOST) and INFLUX_TOKEN.")
        return 1
    try:
        health = requests.get(c["url"] + "/health", timeout=10)
        print("\n/health -> HTTP %d %s" % (health.status_code, health.text[:120]))
    except requests.RequestException as e:
        print("\nCannot reach %s: %s" % (c["url"], e))
        return 1

    soc = latestSocPercent()
    print("latest soc_percent: %s" % ("%.1f %%" % soc if soc is not None else "no sample in the last 30 min"))

    profile, ndays = hourlyAvgProfileWh(FIELD_LOAD, days=7)
    print("\n7-day mean hourly LOAD (Wh), from %d day(s) of samples:" % ndays)
    for h in range(0, 24, 4):
        print("   " + "  ".join("%s:%6d" % (profile[i][0], profile[i][1]) for i in range(h, min(h + 4, 24))))
    print("   daily total: %d Wh" % sum(v for _, v in profile))

    pv, _ = hourlyAvgProfileWh(FIELD_PV, days=7)
    print("\n7-day mean hourly PV (Wh): daily total %d Wh" % sum(v for _, v in pv))
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
