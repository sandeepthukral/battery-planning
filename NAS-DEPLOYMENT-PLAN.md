# Moving the planner to the NAS

Status: **planned, not started.** Written 2026-07-28.

Blocked on the `alphaess-collector` repo finishing its reorganisation — that side owns the
InfluxDB, the Grafana, and the docker network this plan attaches to. See
[Cross-repo contract](#cross-repo-contract-alphaess-collector) for exactly what is needed
from it.

---

## Why

The planner must run unattended every 3 hours. A laptop that sleeps cannot do that.

The Synology **DS220+** already runs the alphaess-collector stack: InfluxDB (the source of
every actual), the collector, and a provisioned Grafana pointed at the same bucket. Putting
the planner beside them removes the LAN-IP and token plumbing entirely, and turns
plan-vs-actual from "compare a text file against a dashboard" into one query against one
database.

**DS220+ is an Intel Celeron J4025 → x86_64**, so PuLP's bundled CBC binary works as
shipped. No `coinor-cbc` package, no solver path override. (This was worth checking: a
32-bit ARM Synology has no CBC wheel at all.)

## Decisions

| Topic | Decision |
|---|---|
| Layout | `battery-planning` keeps its own repo, `Dockerfile` and `docker-compose.yml`; joins the collector's network as `external: true` |
| Influx connection | `INFLUX_URL=http://influxdb:8086` over the docker network — no LAN IP, no `INFLUX_HOST` |
| Bucket | Separate bucket `planning`; reads `alphaess` |
| Scheduling | DSM Task Scheduler as root, mirroring the existing `alphaess-collector/scripts/daily-savings.sh` pattern |
| Output | **Grafana panel**, fed by plan storage. Text plans/logs still written, as a byproduct |
| macOS | `run-matrix.sh` stays zsh/macOS — it is a backtest harness over a fixed past year, it does not move |
| Execution | Still **none**. Advice only. Nothing is sent to the battery |

---

## Cross-repo contract (`alphaess-collector`)

Agreed with that repo's session. Reproduced here so this plan stands alone.

### Bucket: `planning`

Not "battery-planning" — `alphaess` *is* the battery, so "battery" distinguishes nothing.
The real split is **`alphaess` = what happened, `planning` = what we intended.**

Separate bucket, for a concrete reason rather than a preference. The plan data has a growth
shape the actuals do not:

```
measurement: plan
  tag    plan_run   RFC3339 of when the plan was made   <- a new series every run
  _time             the interval planned for
  fields            soc_wh, charge_wh, discharge_wh, import_wh, export_wh,
                    price_buy, price_sell, pv_forecast_wh, load_forecast_wh, reserve_wh
```

`plan_run` has to be a tag, because Grafana must filter on "the current plan". That means
**8 new series/day → ~2,900/year → ~29k series/year across 10 fields, growing forever**, on
a NAS with 2 GB of RAM.

Plans are disposable after a few months; the alphaess history is not. A separate bucket
allows **~400-day retention on `planning`** while `alphaess` stays infinite. Retention is
per-bucket, so in a shared bucket this is simply impossible. That settles it on its own.

Volume itself is small: ~96 intervals × 8 runs ≈ 770 points/day.

### Token: one token, two scopes

**`read:alphaess` + `write:planning`.** Not a `planning`-only token.

The planner reads from `alphaess` on every single run:

| field | used for |
|---|---|
| `soc_percent` | the starting SoC — the plan is built from it and **refuses to run without it** |
| `load_power_w` | the 7-day load profile; this is the *only* load forecast that exists |
| `pv_power_w` | PV forecast calibration |
| `battery_power_w`, `grid_power_w` | plan-vs-actual comparison |

### Grafana: one datasource with read on both buckets

Overlaying planned SoC on measured SoC is the entire point of the project.

Two narrowly-scoped datasources can share a panel via Grafana's "Mixed" mode for simple
overlays, but **a Flux `join()` cannot cross datasources** — only buckets. Computed
plan-vs-actual error and PV calibration both need real joins. Cross-*bucket* joins inside
one datasource are verbose but work; cross-*datasource* joins are not possible at all.

So: one datasource, token readable on both buckets.

### What this repo needs from that one

- the docker network name (expected `alphaess-collector_alphaess-net`, but compose prefixes
  by project directory — confirm with `docker network ls | grep alphaess`)
- the `planning` bucket created, with retention set
- the token above
- a mount line in *that* repo's `docker-compose.yml` for the new dashboard JSON, alongside
  the four existing ones

---

## 1. Portability fixes

**Do these first. Nothing works without them, and each is a silent failure on Linux rather
than a loud one.**

### 1.1 `plan-now.sh` is macOS-only

| line | now | needs |
|---|---|---|
| 1 | `#!/bin/zsh` | `#!/bin/bash` — `python:3.12-slim` has no zsh |
| 20 | `date -v+1d` | `date -d tomorrow` — **BSD-only flag** |
| 29, 54, 61 | `print` | `printf` — zsh builtin, absent in bash |
| 13 | `PY=.venv/bin/python` | `python` — the checked-in `.venv` is a darwin build and must never enter the image |

The `date` one is the dangerous one. On Linux it fails to an **empty** `BT_END`, and an
empty env var falls straight through `_ask()` into `input()` — see next item.

### 1.2 `_ask()` must refuse to prompt when there is no terminal

`Marstek-planning.py:359` returns `input(prompt)` whenever the env var is missing **or
empty**. In a scheduled container that either raises a bare `EOFError` traceback or blocks
forever with nothing reporting it.

Add an `isatty()` guard that raises a named error instead. This converts every future
empty-variable bug from a hang into a message — the same class of fix as the fail-loudly PV
guard and the `BT_INITCHARGE=influx` refusal already in place.

### 1.3 Timezone

Set `TZ=Europe/Amsterdam` in the container and install `tzdata`.

This is correctness, not cosmetics. `Marstek-planning.py:1415` decides whether tomorrow's
prices should exist yet from a **naive** `datetime.now().hour >= 15`. Under UTC that
threshold lands at 17:00 local — so **the 14:05 run, whose entire purpose is to catch the
13:00 price release, would plan a short horizon and not say so.**

Also naive and date-bearing:

- `:215` `today = date.today()` — feeds the `BT_START` default and the
  `runDate.date() == today` gates at `:1435` and `:1938`, where an off-by-one day
  **silently drops PV from the plan**.
- `:372`, `:410` — `datetime.now()` for the default start hour.

Widen `influx_source.py:48`: it catches only `ImportError`, so a missing tzdata would crash
hard there instead of falling back.

### 1.4 Add `requirements.txt`

Dependencies currently exist only inside `.venv`, discoverable nowhere else:

```
requests
pulp
paho-mqtt
```

`paho-mqtt` is imported unconditionally at `Marstek-planning.py:152` even though MQTT is
unused, so it has to be installed.

### 1.5 Add `timeout=` to the outbound calls

`forecast.solar` `:924`, ENTSOE `:979`, EnergyZero `:1172` — all three `requests.get` calls
are currently unbounded. A scheduled job that hangs forever is worse than one that fails,
because nothing surfaces it.

---

## 2. Container

### Dockerfile

`python:3.12-slim`, `tzdata`, non-root user, `requirements.txt`, then the `.py` files and
`plan-now.sh` — mirroring `alphaess-collector/collector/Dockerfile`.

Add a **build-time CBC smoke test** that solves a two-variable LP. x86_64 should be fine,
but proving it at build beats discovering it at 02:05.

### docker-compose.yml

```yaml
services:
  planner:
    build: .
    environment:
      INFLUX_URL: http://influxdb:8086
      INFLUX_TOKEN: ${INFLUX_TOKEN}
      INFLUX_ORG: ${INFLUX_ORG:-home}
      INFLUX_BUCKET: ${INFLUX_BUCKET:-alphaess}     # read: actuals
      INFLUX_PLAN_BUCKET: ${INFLUX_PLAN_BUCKET:-planning}   # write: plans
      TZ: Europe/Amsterdam
      PYTHONPATH: /app
    volumes:
      - ./data:/data
    networks: [alphaess-net]

networks:
  alphaess-net:
    name: alphaess-collector_alphaess-net    # confirm on the NAS
    external: true
```

Joining this network also inherits its **MTU 1400 cap**. That matters: the collector needed
it because the NAS uplink drops full-size TLS handshake packets, surfacing as intermittent
`SSL: UNEXPECTED_EOF_WHILE_READING`. The planner makes the same kind of outbound HTTPS
calls and would hit the same intermittent failure on a default 1500-MTU network.

### Working directory and data

Everything the planner writes is CWD-relative. So:

- code at **`/app`**, with `PYTHONPATH=/app` — needed because `advise.py:170` and
  `influx_to_backtest_csv.py:24` import `influx_source` **without** the `sys.path` guard
  that `Marstek-planning.py:203` has
- **`WORKDIR=/data`**, bind-mounted to `/volume1/docker/battery-planning/data`

That single mount then holds `price_cache/`, `pv_cache/`, `plans/`, `logs/`, plus the
CWD-level `entsoe-output*.txt` and `solarforecast.json`.

**The mount must be writable by the container user.** `Marstek-planning.py:935` and `:1179`
wrap their `os.makedirs` in a bare `except: pass`, so a root-owned mount fails *silently*
and every run refetches — which against forecast.solar's 12/hour budget means the
fail-loudly guard starts firing instead, and the cause looks like a rate limit rather than a
permissions problem. Chown the mount, and verify a cache file actually appears after run 1.

### Seed the caches

**Copy `price_cache/` (500+ files) to the NAS** so the cached year of EnergyZero prices is
not refetched. `pv_cache/` starts empty, which is fine — and the NAS is a new source IP, so
the forecast.solar rate-limit budget starts clean.

---

## 3. Scheduling on DSM

New `scripts/plan.sh` in this repo, modelled on
`alphaess-collector/scripts/daily-savings.sh`:

- set a minimal-PATH-safe `PATH` (DSM Task Scheduler runs with almost none)
- `cd /volume1/docker/battery-planning`
- `docker compose run --rm planner`

DSM **Control Panel → Task Scheduler → Create → Scheduled Task → User-defined script**:

- **General**: user `root` — DSM's docker socket needs it
- **Schedule**: Daily, first run **02:05**, repeat every 3 hours → 02/05/08/11/14/17/20/23
- **Task Settings → Run command**: `/volume1/docker/battery-planning/scripts/plan.sh`

The 14:05 slot is the point of the offset: it is the first run that sees tomorrow's prices,
published around 13:00.

Test by hand once before enabling.

Then **delete `~/Library/LaunchAgents/com.sandeep.battery-planner.plist`** on the Mac. It
was written but never loaded, so there is nothing to unload — just remove the file.

---

## 4. Write every plan into InfluxDB

*(This was Phase 3 of the older plan; it lands here because it only becomes natural once the
planner is on `alphaess-net`.)*

Add `influx_source.writePoints()` over `/api/v2/write` in line protocol, reusing `config()`
— no new dependency, and the token permits it. Call it after the solve.

Schema as given in [the contract](#bucket-planning) above.

Set the **~400-day retention on `planning`** at the same time, not later. The tag grows
without bound and nobody will be watching.

---

## 5. Grafana panel

**Cross-repo:** the dashboard JSON goes in `alphaess-collector/grafana/` and needs a mount
line added to *that* repo's `docker-compose.yml`, alongside the four existing dashboards.
The provisioning provider already picks up anything in `/var/lib/grafana/dashboards`.

`alphaess-battery-plan.json`, four panels:

1. **Planned SoC vs actual SoC**, next ~36 h. The plan line running ahead of the measured
   line is the whole story in one picture.
2. **Planned charge/discharge** as bars against the **buy/sell price** line, so the reason
   for each action is visible.
3. **Action table** — the `advise.py` blocks: from, to, action, kWh, W setpoint, ct/kWh.
4. **Plan age** — a stat panel showing time since the newest `plan_run`. If the scheduled
   task dies, the dashboard says so instead of quietly showing a stale plan.

---

## Verification

| step | check |
|---|---|
| 1 | After the bash rewrite, `plan-now.sh` still produces a plan **on the Mac**. Then blank `BT_END` and confirm the `isatty` guard raises a named error rather than hanging |
| 2 | `docker compose build` succeeds and the CBC smoke test passes. First `docker compose run --rm planner` produces a plan, and `data/pv_cache/` + `data/plans/` contain new files afterwards — this is what proves the mount is writable, which is the silent-failure case |
| TZ | Run the container at 13:30 and 14:30 local; confirm the 14:30 run reports the **longer** horizon. This is the bug that would otherwise hide indefinitely |
| 3 | Trigger the DSM task by hand; confirm a plan appears with the right local timestamp. Then let one scheduled run fire unattended and read the log |
| 4 | Run twice within an hour; confirm two distinct `plan_run` tags coexist and neither overwrites the other |
| 5 | Panel shows the current plan, and after a few hours the actual SoC line visibly diverging from it |
| regression | The Domoticz guard must still report **0 calls attempted** — now from inside the container |

---

## Order, and what is blocking

```
1 (portability)  ->  2 (container)  ->  3 (DSM schedule)  ->  4 (store plans)  ->  5 (Grafana)
```

Step 1 is entirely local to this repo and can start immediately — it does not wait on the
collector reorg. Steps 2–5 all touch the other repo's network, bucket, token, or Grafana.

**Before any of this can be deployed, this repo has to be pushed.** The move to the NAS is a
`git clone`, and nothing from the current working session has been committed:
`Marstek-planning.py`, `run-matrix.sh` and `.gitignore` are modified, and `advise.py`,
`plan-now.sh`, `influx_source.py`, `influx_to_backtest_csv.py`, `clean_backtest_csv.py` are
untracked.

## Still open, unrelated to the move

- **Energy tax is a single global `BT_ETAX`** and is year-dependent (0.12286 in 2025,
  0.11085 in 2026). `plan-now.sh` picks it by year and warns for 2027; the planner itself
  does not. Same class of silent wrongness that date-derived saldering fixed.
- **`pvOverallCalibration` is an unfitted 1.00.** Forecast said 16.9 kWh for 29 July against
  25.1 kWh actual on 28 July. Calibration becomes possible once plans and forecasts are
  being stored — i.e. after step 4.
