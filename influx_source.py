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

Configuration is resolved in two steps, first hit wins:

    1. the real environment          (docker-compose, plan-now.sh, an export)
    2. this repo's own .env          (see .env.example; INFLUX_ENV_FILE overrides the path)

There used to be a third step that read ../../alphaess-collector/.env directly, so a Mac
checkout sitting beside that repo needed no token of its own. It is gone. This repo has no
business reading another one's private file, the path was a guess about directory layout
that held only on one machine, and the coupling was silent in the worst way: after the
collector split its admin token into four scoped ones, that fallback would happily hand
back the admin token while the correctly-scoped one sat beside it.

Copy the token into this repo's .env instead. One line, once, per machine.

    INFLUX_URL       e.g. http://influxdb:8086        (required, or INFLUX_HOST)
    INFLUX_HOST      host only; INFLUX_PORT defaults to 8086
    INFLUX_TOKEN     API token                        (required)
                     INFLUX_TOKEN_PLANNING is accepted too, and preferred where both
                     appear - that is the name the collector gives the scoped token
                     for this planner (read:alphaess + write:planning)
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
except Exception:                                       # pragma: no cover
    # ImportError only covers "no zoneinfo module". The likelier failure in a slim container
    # is ZoneInfoNotFoundError: the module imports fine but the system has no tzdata, so the
    # name cannot be resolved. Catching only ImportError turns that into a hard crash at
    # import time, before anything can report why.
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
    """Resolve connection settings: the real environment wins over this repo's .env."""
    global _config
    if _config is None:
        env_file = os.environ.get("INFLUX_ENV_FILE", DEFAULT_ENV_FILE)
        fromfile = _read_env_file(env_file)

        def pick(key, default=""):
            # real environment, then this repo's .env. An empty value falls through rather
            # than winning, so a commented-out or blanked key does not shadow a real one.
            return os.environ.get(key) or fromfile.get(key) or default

        def pickToken():
            # alphaess-collector calls the planner's token INFLUX_TOKEN_PLANNING - read on
            # alphaess, write on planning. Accept that name as well as the generic one, so
            # the line can be copied out of its .env verbatim rather than renamed in transit.
            # Within a source the specific name wins, on the principle that a token named for
            # this job beats one that merely might be for it.
            for source in (os.environ, fromfile):
                for key in ("INFLUX_TOKEN_PLANNING", "INFLUX_TOKEN"):
                    value = source.get(key)
                    if value:
                        return value
            return ""

        url = pick("INFLUX_URL")
        if not url:
            # in a container INFLUX_URL carries the service alias; on a laptop only a host
            # is usually known, so accept that form and supply the default port
            host = pick("INFLUX_HOST")
            port = pick("INFLUX_PORT", "8086")
            url = "http://%s:%s" % (host, port) if host else ""
        _config = {
            "url": url.rstrip("/"),
            "token": pickToken(),
            "org": pick("INFLUX_ORG", "home"),
            "bucket": pick("INFLUX_BUCKET", "alphaess"),
            "sys_sn": pick("ALPHAESS_SYS_SN"),
            "poll_seconds": float(pick("POLL_INTERVAL_SECONDS", "30") or 30),
            # Plans go in their own bucket, not beside the actuals. The collector's scoped
            # token is read:alphaess + write:planning, so this separation is enforced by the
            # credential as well as by convention - the planner cannot overwrite a measurement.
            "plan_bucket": pick("INFLUX_PLAN_BUCKET", "planning"),
            "env_file": env_file,
        }
    return _config


def resetConfig():
    """Forget the cached config so the next config() call re-reads the environment.

    Only needed by tests: a real process reads its environment once at startup, so
    caching is the right call there. A test process does not get that fresh start -
    without this, a test that sets INFLUX_URL after any earlier test has already called
    config() would silently see the earlier test's answer instead of its own.
    """
    global _config
    _config = None


def configured():
    c = config()
    return bool(c["url"] and c["token"])


def _query(flux):
    """POST a Flux query, return parsed rows as dicts. Raises on transport failure."""
    c = config()
    if not configured():
        # Name what is missing and every place that was searched. The original message
        # mentioned only INFLUX_ENV_FILE, which pointed at the wrong fix in a container:
        # there the answer is to pass the token through docker-compose, not to repath a .env.
        missing = [k for k, v in (("INFLUX_URL (or INFLUX_HOST)", c["url"]),
                                  ("INFLUX_TOKEN (or INFLUX_TOKEN_PLANNING)",
                                   c["token"])) if not v]
        raise RuntimeError(
            "InfluxDB is not configured: missing %s.\n"
            "  Searched: the environment, then %s.\n"
            "  Copy .env.example to .env and fill it in, or set the variables directly "
            "(in a container, pass them from docker-compose)."
            % (" and ".join(missing), c["env_file"]))
    resp = requests.post(
        c["url"] + "/api/v2/query",
        params={"org": c["org"]},
        data=flux.encode("utf-8"),
        headers={"Authorization": "Token " + c["token"],
                 "Content-Type": "application/vnd.flux",
                 "Accept": "application/csv"},
        timeout=30)
    resp.raise_for_status()
    return _parseAnnotatedCsv(resp.text)


def _parseAnnotatedCsv(text):
    """Parse InfluxDB's annotated CSV, which is several tables in one document.

    Not csv.DictReader over the whole response. Influx starts a new annotation block --
    #datatype, #group, #default, then a fresh header row -- every time the result schema
    changes, and DictReader binds the first header for the whole document. The later header
    rows then arrive as data, giving a row whose _time is the string "_time"; and if the
    schemas really do differ, every data row after the change is read against the wrong
    column names while still looking like a perfectly ordinary row.

    Latent until 2026-07-30, when adding pv_forecast_raw_wh to the plan measurement meant a
    single query could span runs written before and after the change. Handling the blocks
    is what makes adding a field to a measurement a non-event.
    """
    rows = []
    header = None
    for raw in _csv.reader(io.StringIO(text)):
        if not raw or all(cell == "" for cell in raw):
            header = None                       # blank line ends a block
            continue
        if raw[0].startswith("#"):              # annotation line
            continue
        # A header row carries the literal "result" in the column of that name; a data row
        # carries the yield name there. Checked as well as the blank-line reset because the
        # first block has no blank line before it.
        if header is None or (len(raw) > 1 and raw[1] == "result"):
            header = raw
            continue
        row = dict(zip(header, raw))
        if row.get("_time") in (None, "") and row.get("_value") in (None, ""):
            continue
        rows.append(row)
    return rows


def _escapeTagPart(value):
    """Escape a measurement name, tag key or tag value for line protocol."""
    return str(value).replace("\\", "\\\\").replace(",", "\\,").replace(" ", "\\ ").replace("=", "\\=")


def linePoint(measurement, tags, fields, timestamp):
    """Build one line-protocol record. timestamp is an aware datetime; precision is seconds.

    Fields are written as floats, including the ones that are conceptually whole watt-hours.
    Mixing int (1i) and float for the same field name in the same measurement makes InfluxDB
    reject the later type outright, and a field that is 0 on a dull day and 0.5 on a bright
    one would do exactly that. One type everywhere costs nothing and cannot collide.
    """
    parts = [_escapeTagPart(measurement)]
    for key in sorted(tags):
        if tags[key] in (None, ""):
            continue
        parts.append("%s=%s" % (_escapeTagPart(key), _escapeTagPart(tags[key])))
    fieldParts = []
    for key in sorted(fields):
        value = fields[key]
        if value is None:
            continue
        fieldParts.append("%s=%s" % (_escapeTagPart(key), float(value)))
    if not fieldParts:
        raise ValueError("line protocol needs at least one field (measurement %s)" % measurement)
    if timestamp.tzinfo is None:
        # A naive timestamp is read as local time, which is what every wall-clock value in
        # this project means. Only when tzdata is missing entirely does UTC stand in, so the
        # points still land somewhere defined rather than following the process's idea of local.
        timestamp = timestamp.replace(tzinfo=LOCAL_TZ or timezone.utc)
    return "%s %s %d" % (",".join(parts), ",".join(fieldParts), int(timestamp.timestamp()))


def writePoints(lines, bucket=None, batch=1000):
    """POST line-protocol records to /api/v2/write. Returns how many lines were written.

    Raises on any failure. The caller decides whether that is fatal - for the planner it is
    not: the plan text file is already on disk by then and losing the InfluxDB copy costs a
    dashboard point, not the advice.
    """
    c = config()
    if not configured():
        raise RuntimeError("InfluxDB is not configured; cannot write. Searched the "
                           "environment, then %s." % c["env_file"])
    target = bucket or c["plan_bucket"]
    written = 0
    for start in range(0, len(lines), batch):
        chunk = lines[start:start + batch]
        resp = requests.post(
            c["url"] + "/api/v2/write",
            params={"org": c["org"], "bucket": target, "precision": "s"},
            data="\n".join(chunk).encode("utf-8"),
            headers={"Authorization": "Token " + c["token"],
                     "Content-Type": "text/plain; charset=utf-8"},
            timeout=30)
        if resp.status_code >= 400:
            # Influx puts the useful part in the body - which field collided, which line
            # failed to parse - and raise_for_status() throws all of it away.
            raise RuntimeError("write to bucket %r failed: HTTP %d %s"
                               % (target, resp.status_code, resp.text[:400]))
        written += len(chunk)
    return written


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


def _rangeClause(start, stop):
    return '|> range(start: %s, stop: %s)' % (
        start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        stop.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


def _windowStarts(rows, minutes):
    """Relabel aggregateWindow output by window START, in local time.

    aggregateWindow stamps each window with its END. Everything in this project labels a
    period by when it begins, so step back one window width.
    """
    out = {}
    for row in rows:
        try:
            ts, value = row["_time"], float(row["_value"])
        except (KeyError, ValueError):
            continue
        out[(_parse_time(ts) - timedelta(minutes=minutes)).astimezone(LOCAL_TZ or timezone.utc)] = value
    return out


def intervalEnergyWh(field, start, stop, minutes=60, min_coverage=0.5, clamp_negative=False):
    """Energy in Wh per interval, keyed by interval start as an aware local datetime.

    start/stop are aware datetimes. Intervals whose sample coverage falls below
    min_coverage are omitted rather than reported as a quiet one - a collector outage and
    an empty house look identical in the mean, and only the count tells them apart.

    minutes exists because plans are quarter-hourly (NL day-ahead moved to a 15-minute MTU)
    while this function was originally hourly. Comparing a plan against actuals at hourly
    resolution would average away exactly the short price spikes the planner is built to
    catch. Windows align to the epoch in UTC, which lands on local quarters and hours too,
    because every Amsterdam offset is a whole number of hours.

    clamp_negative floors samples at zero before averaging. Use it for load and PV,
    which cannot physically be negative but do occasionally report so during fast
    power swings (~0.07% of load samples). Never use it for battery_power_w or
    grid_power_w, where the sign carries the direction of flow.
    """
    c = config()
    base = 'from(bucket: "%s")\n  %s\n  |> filter(fn: (r) => r._measurement == "%s" and r._field == "%s")%s' % (
        c["bucket"], _rangeClause(start, stop), MEASUREMENT, field, _sys_filter())
    if clamp_negative:
        base += '\n  |> map(fn: (r) => ({r with _value: if r._value < 0.0 then 0.0 else r._value}))'

    every = "%dm" % int(minutes)
    means = _windowStarts(_query(base + '\n  |> aggregateWindow(every: %s, fn: mean, createEmpty: false)' % every), minutes)
    counts = _windowStarts(_query(base + '\n  |> aggregateWindow(every: %s, fn: count, createEmpty: false)' % every), minutes)

    expected = (minutes * 60.0) / c["poll_seconds"] if c["poll_seconds"] else minutes * 2.0
    return {t: watts * minutes / 60.0            # mean W over the window -> Wh
            for t, watts in means.items()
            if counts.get(t, 0.0) / expected >= min_coverage}


def intervalLastValue(field, start, stop, minutes=60):
    """Last sample of a field in each interval, keyed by interval start (aware local).

    For state fields - soc_percent - where the value at the end of an interval is the
    meaningful one and averaging would blur a trajectory. Matches how the planner reports
    SoC: its schedule records the level reached at the end of each interval.
    """
    c = config()
    base = 'from(bucket: "%s")\n  %s\n  |> filter(fn: (r) => r._measurement == "%s" and r._field == "%s")%s' % (
        c["bucket"], _rangeClause(start, stop), MEASUREMENT, field, _sys_filter())
    return _windowStarts(
        _query(base + '\n  |> aggregateWindow(every: %dm, fn: last, createEmpty: false)' % int(minutes)),
        minutes)


def planPoints(start, stop, measurement="plan"):
    """Stored plan intervals starting in [start, stop), one dict per point.

    Every run writes a full horizon, so an interval is normally covered by several plans -
    the 02:05 run and the 14:05 run both have an opinion about 18:00. They are all returned,
    each carrying its plan_run tag; deciding which one was in force is the caller's job.

    Returns [{"plan_run": str, "time": aware local datetime, "soc_wh": float, ...}].

    Also reads measurements in this bucket that carry no plan_run at all - plan_score is
    one series, deliberately untagged - in which case plan_run comes back empty.
    """
    c = config()
    flux = '''from(bucket: "%s")
  %s
  |> filter(fn: (r) => r._measurement == "%s")
  |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")''' % (
        c["plan_bucket"], _rangeClause(start, stop), measurement)
    out = []
    for row in _query(flux):
        if not row.get("_time"):
            continue
        point = {"plan_run": row.get("plan_run") or "",
                 "time": _parse_time(row["_time"]).astimezone(LOCAL_TZ or timezone.utc)}
        for key, value in row.items():
            if key in ("result", "table", "_time", "_start", "_stop", "_measurement", "plan_run"):
                continue
            try:
                point[key] = float(value)
            except (TypeError, ValueError):
                continue
        out.append(point)
    return out


def hourlyEnergyWh(field, start, stop, min_coverage=0.5, clamp_negative=False):
    """Hourly energy in Wh for a power field, keyed "YYYY-MM-DD HH" in local time.

    The string-keyed hourly view kept for callers that shape data for the planner. New code
    wanting another resolution, or datetime keys, should call intervalEnergyWh directly.
    """
    hours = intervalEnergyWh(field, start, stop, 60, min_coverage, clamp_negative)
    return {t.strftime("%Y-%m-%d %H"): v for t, v in hours.items()}


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
    print("  bucket   : %s (read)" % c["bucket"])
    print("  plans to : %s (write)" % c["plan_bucket"])
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
