# Moving the planner to the NAS

Status: **planned, not started.** Written 2026-07-28, revised 2026-07-30.

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

## 1. Portability fixes — **COMPLETE**

Each of these was a silent failure on Linux rather than a loud one. All are done and
verified; a full-month backtest is byte-identical to `main`, so nothing here changed what the
planner decides — only where it can run.

### 1.1 `plan-now.sh` is macOS-only — **DONE**

`plan-now.sh` and `solar-forecast.sh` are both `#!/bin/bash` now, with `print` → `printf`
and `[[ ]]` → `[ ]` throughout. Verified under `bash -n`, `zsh -n` **and** a real run.

| was | now |
|---|---|
| `#!/bin/zsh` | `#!/bin/bash` — `python:3.12-slim` has no zsh |
| `date -v+1d` | try GNU, fall back to BSD — **see below, the plan was wrong here** |
| `print` | `printf '%s\n'` |
| `PY=.venv/bin/python` | `.venv/bin/python` if present, else `python3` |
| `INFLUX_HOST=192.168.68.105` | only defaults when `INFLUX_URL` is unset |
| `$pipestatus[1]` | pipeline removed entirely |
| *(new)* | `export TZ=$BT_TZ`, so shell dates and the planner share one clock |

**Correction: `date -d tomorrow` is not the fix.** This plan previously said to switch
outright. That fixes Linux and *breaks the Mac*, where `plan-now.sh` is run by hand — BSD
date rejects `-d` with `date: illegal option -- d` and exits 1. GNU rejects `-v` the same
way. There is no shared spelling, so:

```sh
tomorrow=$(date -d tomorrow +%Y%m%d 2>/dev/null || date -v+1d +%Y%m%d)
```

with an explicit empty check after it. An empty `BT_END` no longer hangs — `_ask()` warns and
takes a default — but it would still plan over the wrong window, so the script stops instead.

**The `pipestatus` trap, recorded because the fix is not the obvious one.** zsh's array is
1-indexed, bash's is 0-indexed:

```
zsh    ${pipestatus[1]}      bash   ${PIPESTATUS[0]}
```

Same concept, same spelling but for case, *different index*. Porting this file by
mechanically lowercasing `pipestatus` gives `${PIPESTATUS[1]}` — the exit status of `tee`,
which is always 0. Measured:

```
zsh :  $? = 0 (tee)   pipestatus[1] = 1
bash:  PIPESTATUS[0] = 1     PIPESTATUS[1] = 0   <- the naive port
```

The horizon guard would then be present, compiled, tested and **dead**: a starved price fetch
would print `ERROR:` and the run would still exit 0.

Shell detection (`if [ -n "$ZSH_VERSION" ]`) works and was considered. Rejected — it keeps a
silent-failure mode alive to solve a problem that vanishes with the pipe. `plan-now.sh` now
captures the output, keeps `$?` directly, and prints afterwards. `advise.py` emits a few lines
instantly, so nothing needs streaming. Verified identical under zsh, bash and dash.

### 1.2 `_ask()` must not prompt when there is no terminal — **DONE** (`56ed33d`)

`_ask()` used to call `input(prompt)` whenever the env var was missing **or** empty. In a
scheduled container that raises a bare `EOFError` at best and blocks forever at worst.

Now three branches: a set, non-empty variable wins; a variable that is **set but empty**
warns and takes the default (the `date -v+1d` case in 1.1, so a broken caller says so
instead of hiding behind a plausible plan); and with no terminal (`sys.stdin.isatty()` false)
it takes the default silently. That last branch is what makes the constants at the top of
`Marstek-planning.py` the single source of truth for unattended runs.

Verified on the live path and across a full-year backtest: defaults, explicit overrides and
an empty `BT_CAP=` all produce the expected plan, and no run hangs.

### 1.3 Timezone — **DONE**

The original instruction was "set `TZ=Europe/Amsterdam` in the container and install
`tzdata`". That is still worth doing, but it was the wrong *primary* fix: it makes
correctness depend on a Dockerfile line that nobody rereads, and it is silent when removed.

The program no longer depends on the process timezone. `Marstek-planning.py` gains a
`planningTZ` / `localNow()` / `localToday()` block keyed off `BT_TZ` (default
`Europe/Amsterdam`), and all seven wall-clock reads go through it — including the one that
matters, the `currentHour >= 15` test deciding whether tomorrow's day-ahead should exist.
Under UTC that test fired at 17:00 local, so **the 14:05 run would plan a short horizon and
say nothing**.

The helpers return **naive** datetimes on purpose. The rest of the file compares naive values
throughout; converting wholesale to aware datetimes is a far larger change with real risk of a
silent one-hour error. Attaching the right wall clock to the existing convention fixes the bug
without disturbing it.

`plan-now.sh` also exports `TZ=$BT_TZ`, because the plan filename, `BT_START`, `BT_STARTHOUR`
and the energy-tax year all come from shell `date`, which follows `TZ`. Keyed off `BT_TZ`
rather than `${TZ:-...}` deliberately — the latter would let an image that sets `TZ=UTC` win,
which is the exact case being defended against. One knob, both halves.

Measured:

```
TZ=Europe/Amsterdam   naive now()=12:19   localNow()=12:19
TZ=UTC                naive now()=10:19   localNow()=12:19
TZ=America/Los_Angeles naive now()=03:19  localNow()=12:19
```

A full `TZ=UTC ./plan-now.sh` produces advice **identical** to the normal run and writes
`plans/plan_20260730_12.txt` — the Amsterdam hour, not UTC's 10.

`influx_source.py` now catches `Exception` rather than `ImportError` around its `ZoneInfo`
setup. `ImportError` only covers "no zoneinfo module"; the likely slim-container failure is
`ZoneInfoNotFoundError`, where the module imports fine but no tzdata exists. A bogus `BT_TZ`
now warns and falls back to the system clock instead of crashing — verified.

### 1.4 Add `requirements.txt` — **DONE**

```
requests==2.34.2
pulp==3.3.2
paho-mqtt==2.1.0
tzdata==2026.3
```

Pinned, not floating: the NAS resolves these fresh at image build, months after they were last
exercised, and an unpinned solver picking up a new major is exactly the failure a scheduled job
reports to nobody. The first three are pinned to the versions **verified running here**, not to
guesses. `paho-mqtt` is imported unconditionally even though MQTT is unused, so it is required.
`tzdata` is not imported by name — it is the data behind `zoneinfo`, absent from
`python:3.12-slim`, and without it `ZoneInfo("Europe/Amsterdam")` raises. All four resolve.

### 1.5 Add `timeout=` to the outbound calls — **DONE**

`HTTP_TIMEOUT=(10,30)` (connect, read) on the three live calls: forecast.solar, ENTSOE and
EnergyZero.

The eight Domoticz `requests.get(baseJSON+...)` calls and the notification email are left
unbounded. They are unreachable with `useDomoticz=False` and touching them would expand the
diff into dead code — but if that flag is ever set back to `True`, they need the same
treatment.

### 1.6 `solar-forecast.sh` — **DONE**

Ported alongside `plan-now.sh`: bash shebang, `printf`, `[ ]` tests. It wraps `plan-now.sh`
and summarises the PV forecast by hour.

It is a **convenience script for the Mac**, not part of the scheduled path — it forces a full
replan just to print a forecast, which on a 3-hourly schedule is wasted API budget. Port it
for consistency if it goes in the image at all; do not schedule it. Once plans are stored
(section 4) the same numbers come out of InfluxDB without replanning.

---

## 1b. Configuration that is not in the repo

**This is the part a `git clone` cannot give you, and none of it fails loudly.**

### The InfluxDB token — **RESOLVED**

`influx_source.py` used to read its config from exactly one place: `../../alphaess-collector/.env`,
**relative to itself**. That resolves on the Mac only because both repos sit under
`battery-smart-control-projects/`. From `/app` in a container it resolves to a path that
cannot exist, `_read_env_file()` swallows the miss (`except OSError: pass`), and the run dies
at `BT_INITCHARGE=influx` with a message pointing at `INFLUX_ENV_FILE` — the wrong fix.

`config()` now resolves each key through three sources, first non-empty wins:

| # | source | who uses it |
|---|---|---|
| 1 | the real environment | docker-compose on the NAS, `plan-now.sh`, a manual export |
| 2 | **this repo's `.env`** | the portable answer; documented in `.env.example` |
| 3 | `../../alphaess-collector/.env` | dev convenience on the Mac — no token copied by hand |

Step 3 stays only so a Mac checkout keeps working untouched; it is expected to resolve to
nothing anywhere else. `.env.example` **is committed** and lists every key with both the
`INFLUX_URL=http://influxdb:8086` container form and the `INFLUX_HOST=` LAN form.

The failure message now names what is missing and every path searched, and says to pass the
variables from docker-compose when in a container:

```
InfluxDB is not configured: missing INFLUX_URL (or INFLUX_HOST) and INFLUX_TOKEN.
  Searched: the environment, then /nonexistent/.env, then /app/../../alphaess-collector/.env.
  Copy .env.example to .env and fill it in, or set the variables directly (in a
  container, pass them from docker-compose).
```

**At deployment:** `cp .env.example .env` on the NAS and fill in `INFLUX_TOKEN` with the
`read:alphaess` + `write:planning` token from the cross-repo contract. That file is gitignored
and does not travel — it is created by hand, once, on the NAS. Passing the same variables
through `docker-compose.yml` instead is equally valid and takes precedence.

### `.env` in this repo holds a KNMI key and travels nowhere

A `.env` now exists in the repo root, correctly gitignored, containing one key:

```
KNMI_API_KEY
```

**Nothing in this repo reads it** — verified, zero references to `KNMI` in any tracked file.
It is a credential parked ahead of the PV-forecast work (section 7). Consequences:

- it will not travel with the clone, which is right;
- it is also **single-copy**, like the data in section 2b. It is retrievable — KNMI Data
  Platform → API Catalog → Open Data API → "Request an API key", shown once on screen — so
  losing it costs a re-request, not a dead end;
- because no code reads it, its absence on the NAS breaks nothing today. Do not add it to
  the container until the forecast work actually lands.

### Summary of what must exist on the NAS but is not in git

| item | where it goes | if missing |
|---|---|---|
| `INFLUX_TOKEN` (read `alphaess`, write `planning`) | NAS `.env`, from `.env.example` | run refuses at `BT_INITCHARGE=influx`, now with an actionable message |
| `INFLUX_URL=http://influxdb:8086` | same `.env`, or compose | falls back to the LAN IP, which hairpins or fails from inside the container |
| docker network name | `docker-compose.yml`, confirmed on the NAS | container will not start |
| `battery-data/` | outside the checkout | irreplaceable history lost — see 2b |
| `KNMI_API_KEY` | nowhere yet | nothing, until the PV-forecast work is built |

`.env.example` is committed and carries all of the above except the network name, so the NAS
side is `cp .env.example .env` plus filling in one token.

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

**Do not bother copying `price_cache/`.** An earlier version of this plan said to move all
500+ files so the cached year of EnergyZero prices was not refetched. That advice is now
wrong, and the reason is worth recording because it was a real bug.

The price cache used to be trusted unconditionally. A **live** run's cache key is today's
date, so an entry written at 08:00 — before the day-ahead auction publishes around 13:00 —
would be read back forever, and every later run that day would plan on a horizon that
permanently lacked tomorrow's prices. `getPricesFromEnergyZero()` now only reads from disk
when `rundate < today`; today and future always refetch and overwrite.

The NAS runs the **live path only** (`run-matrix.sh` stays on macOS by the decision above),
so every run there is a today-or-later run, and the cache on the NAS is **written but never
read**. Copying 30 MB across to be ignored achieves nothing. The historical year still
matters on the Mac, where backtests run — it is listed under "What actually travels" for
that reason, not this one.

`pv_cache/` starts empty, which is fine. The NAS is also a new source IP, so the
forecast.solar rate-limit budget starts clean.

---

## 2b. Irreplaceable data

Not a deployment step, but it belongs here because the NAS is where the second copy should
land, and because the move is the moment it is easy to do.

Household data is deliberately **not** in this repo: it is a fork of a public upstream, so
anything committed is world-readable, and a year of hourly load is occupancy data — it shows
when the house is empty and which weeks we were away. That decision is right, and it has a
consequence: git is not the backup.

Everything the project depends on is regenerable **except two things**:

| data | size | if lost |
|---|---|---|
| **`backtest_input_hourly.csv`** | 292 KB | **Gone permanently** |
| `backtest_input_hourly_clean.csv` + sidecar | 300 KB | `python3 clean_backtest_csv.py` |
| `results-raw-baseline/`, `results/` | 26 MB | one `./run-matrix.sh` |
| `price_cache/` | 30 MB | re-fetchable from EnergyZero (~522 calls) |
| **Sparky P1 export** | 115 MB | **Gone permanently** — no re-export will be done |
| `pv_cache/` | 16 KB | worthless, 48 h retention |

The APsystems EMA export covers **2025-07-01 → 2026-06-30**. InfluxDB history starts
**2026-07-17** — seventeen days *after* it ends, so there is **no overlap** and Influx cannot
regenerate any of it. The collector was not running. Every financial conclusion this project
has produced traces back to that 292 KB.

The Sparky export is the second one. It *was* re-downloadable, and an earlier version of this
section said so; that is no longer true — the decision is that no further export will be taken
from Sparky. So the 115 MB directory under `battery-data/` is the only copy of the P1 record,
and the P1 half of the cross-validation dies with it. Treat it exactly like the EMA CSV.

### What the P1 data changes

```
EMA CSV      2025-07-01 ─────────────────────────► 2026-06-30
P1                        2026-01-22 ───────────────────► 2026-07-24
InfluxDB                                            2026-07-17 ──────► now
```

`p1_to_backtest_csv.py` turns the Sparky export into the same `datetime,load_kwh,solar_kwh`
format via `load = solar + delivery − return`. This gives:

- a **second, independent measurement** of 2026-01-22 → 06-30, so that stretch is no longer
  single-copy;
- coverage of **2026-07-01 → 07-16**, the gap between the EMA export ending and InfluxDB
  starting (Phase 6 in the main plan) — pending an hourly solar series for those days, since
  P1 sees only the net at the meter.

**It is truncated at 2026-07-16** (`--until`, the default). The identity holds only while
nothing but the house is on the connection. Once the battery runs,
`load = solar + delivery − return + discharge − charge` and P1 alone cannot separate the
terms. Data past that date is not noisier, it is wrong.

Agreement with the EMA export on monthly energy is **1.3–1.9%** once the one-sided exclusion
of impossible negative hours is adjusted for — two independent measurement chains, a utility
meter and the AlphaESS CT. The tool prints this table on every run rather than hiding it. The
unadjusted column runs ~3% high because EMA misplaces solar in time without losing it, so its
low hours surface as negative loads and get dropped while the high hours stay.

**Still single-copy: 2025-07-01 → 2026-01-21.** Roughly 6.5 months including a full winter —
the season the reserve logic leans on hardest. P1 does not reach back that far.

### Where the copies live

Moved out of `~/Downloads` (a folder people empty, often excluded from backups) to
`/Users/sandeep/Personal/battery-data/`:

```
backtest_input_hourly.csv                     the irreplaceable year
backtest_input_hourly_clean.excluded.json     which days were excluded and why
sparky-export-20260724/                       full Sparky export, 115 MB
```

The Sparky export carries the meter EAN, the meter number and the service address. It must
never enter the repo — hence the absolute path default in `p1_to_backtest_csv.py`, overridable
with `P1_CSV`.

**To do at deployment:** copy `battery-data/` to the NAS alongside `price_cache/`. Once
plans and actuals are landing in InfluxDB (step 4) the collector becomes the durable record
and this only guards the pre-2026-07-17 past — but that past is the part that cannot be
re-measured by waiting.

### What actually travels to the NAS

"Move the repo to the NAS" does **not** move the data, and this trips people twice over.

**`git clone` brings the 11 tracked files, nothing else:**

```
.gitignore  Marstek-planning.py  NAS-DEPLOYMENT-PLAN.md  README.md  advise.py
clean_backtest_csv.py  influx_source.py  influx_to_backtest_csv.py
p1_to_backtest_csv.py  plan-now.sh  run-matrix.sh
```

**Trap 1 — the P1 export was never inside the repo folder.** It lives at
`/Users/sandeep/Personal/battery-data/`, a sibling of the git checkout entirely. Copying the
repo directory, however thoroughly, does not touch it.

**Trap 2 — even the data that *is* inside the repo folder is gitignored**, so a clone leaves
it behind: `backtest_input_hourly.csv`, `backtest_input_hourly_clean.csv` and its sidecar,
`price_cache/`, `results-raw-baseline/`, `plans/`, `logs/`, `pv_cache/`. That is the point of
the `.gitignore` — this fork is public — but it means the ignore rules and the backup plan
pull in opposite directions, and only the second one is a manual step.

Copy out of band, in this order of importance:

| what | from | size | why |
|---|---|---|---|
| `battery-data/` | `~/Personal/battery-data/` | 115 MB | **both irreplaceable sets** — EMA CSV and the Sparky P1 export |
| `backtest_input_hourly.csv` | repo root | 292 KB | working copy; the master is in `battery-data/` |
| `price_cache/` | repo root | 30 MB | saves ~522 EnergyZero calls; goes in the container's `/data` mount |
| `results-raw-baseline/` | repo root | 13 MB | optional — one `./run-matrix.sh` rebuilds it |

`battery-data/` must land **outside** the git checkout on the NAS too, or the next `git add`
will offer to commit the meter EAN and service address to a public repo. Then set `P1_CSV` to
wherever it landed, since the default path in `p1_to_backtest_csv.py` is a Mac path.

A NAS is one machine. Copying to it makes a second copy, not a backup — if the point is
surviving a dead disk, the NAS needs its own snapshot or off-box sync of that directory.

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

### `pv_forecast_wh` earns its place — record it from day one

The schema already carries `pv_forecast_wh`, and it is now the highest-value field in the
list rather than a nice-to-have.

On 2026-07-29 the 02:00 plan forecast **18.50 kWh** of PV against **26.55 kWh** actual — 43%
under, concentrated in the evening (16:00–20:00 ran +57% to +178%). This was investigated and
is **not** a caching bug: raw `pv_cache/*.json` fetches at 07:00, 08:00 and 20:00 all carried
the same ~1120 Wh for 18:00, including the fetch made *during that hour*. forecast.solar's
weather input simply never picked up the clearing.

One day is not a bias, and nothing should be retuned off it — `pvElevationLossCurve` and
`pvOverallCalibration` stay untouched. But the comparison cannot even begin until forecasts
are stored next to actuals, and **every day before that is unrecoverable**: forecast.solar
has no history endpoint, so a forecast not written down at the time is gone. That makes this
field the cheapest item in the whole plan and an argument for not deferring section 4.

Next-day check is a Flux join of `planning.pv_forecast_wh` against `alphaess.pv_power_w`.

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
`git clone`, so anything uncommitted does not exist as far as the NAS is concerned. The
branch `nas-planner-and-grid-limits` carries the planner work; a later round of changes
(price-cache fix, `advise.py --min-hours`, README/`docs/PLAN.md`, `solar-forecast.sh`) still
needs committing on top.

## Still open, unrelated to the move

- **Energy tax is a single global `BT_ETAX`** and is year-dependent (0.12286 in 2025,
  0.11085 in 2026). `plan-now.sh` picks it by year and warns for 2027; the planner itself
  does not. Same class of silent wrongness that date-derived saldering fixed.
- **`pvOverallCalibration` is an unfitted 1.00.** Forecast said 16.9 kWh for 29 July against
  25.1 kWh actual on 28 July. Calibration becomes possible once plans and forecasts are
  being stored — i.e. after step 4.
- **A better PV forecast source, researched but deliberately not built.** Buienradar called
  the 29 July evening correctly where forecast.solar did not, but its free feed has **no
  hourly solar forecast field** — only live `sunpower` (W/m², nearest station Lelystad, which
  is in Flevoland) and a coarse daily `sunChance`. The real source of that edge is KNMI's
  **HARMONIE-AROME** model, dataset `harmonie_arome_cy43_p2b` ("renewable energy
  parameters"), CC-BY-4.0.

  The cost is the catch and is why this is not scheduled: each model run is a **~1.4 GB
  `.tar`** bundling every parameter over the whole NL grid, with no per-location endpoint.
  One radiation number for this lat/lon means downloading it, extracting the GRIB2, and
  decoding with `eccodes`/`cfgrib` — a real system dependency, not pure pip. Every 3 hours on
  a 2 GB NAS that is a different class of job from forecast.solar's single JSON call.

  **Do not start this before step 4 has produced a few weeks of forecast-vs-actual.** One
  43% miss does not establish a systematic bias, and this is far too expensive to build on
  one day of evidence.
