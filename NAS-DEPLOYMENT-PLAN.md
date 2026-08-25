# Moving the planner to the NAS

Status: **planned, not started.** Written 2026-07-28, revised 2026-07-30.

> **Naming note.** The optimiser was called `Marstek-planning.py` when this was written; it was
> renamed to `planner.py` on 2026-08-25. References below keep the old name, because this is the
> record of how the deployment was built rather than current documentation. The DSM Task
> Scheduler entries are unaffected: they invoke `scripts/plan.sh`, whose name did not change.

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
| Looking back | A **day-after report** comparing yesterday's plans against what actually happened — money, outcomes and solar-forecast accuracy, reported separately. Built 2026-07-30 as `report_day.py`; see [section 6](#6-the-day-after-report-plan-vs-what-actually-happened--built-report_daypy) |

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
  fields            soc_wh, charge_wh, discharge_wh, import_wh, export_wh, cost_eur,
                    price_buy, price_sell, pv_forecast_wh, pv_forecast_raw_wh,
                    load_forecast_wh, reserve_wh
```

`pv_forecast_raw_wh` was added on 2026-07-31 and is the same forecast **before**
`pvElevationCalibration()`, `pvOverallCalibration` and `pvPlanningFactor` are applied. Storing
only the product makes two different failures indistinguishable — a forecast that was wrong
and a correction that was wrong call for opposite fixes, and `pvOverallCalibration` (still an
unfitted 1.00) has to be fitted against the raw number. The raw responses live in `pv_cache/`
for 48 hours and are then pruned, so a run that does not record this loses the comparison for
good. One extra field on an existing point: no new series, no cardinality cost.

`reserve_wh` is one number for the whole horizon, not a per-interval decision, and is
repeated on every point anyway — that lets a dashboard draw it as a line under `soc_wh`
without a second query and a join.

`plan_run` has to be a tag, because Grafana must filter on "the current plan". That means
**8 new series/day → ~2,900/year → ~29k series/year across 10 fields, growing forever**, on
a NAS with 2 GB of RAM.

Plans are disposable after a few months; the alphaess history is not. A separate bucket
allows **~400-day retention on `planning`** while `alphaess` stays infinite. Retention is
per-bucket, so in a shared bucket this is simply impossible. That settles it on its own.

Volume itself is small: ~96 intervals × 8 runs ≈ 770 points/day.

### Token: one token, two scopes

**`read:alphaess` + `write:planning`.** Not a `planning`-only token.

**Delivered 2026-07-30 as `INFLUX_TOKEN_PLANNING`.** The collector replaced its single admin
token with four narrowly-scoped ones; that is the name ours has on its side. `influx_source.py`
reads either `INFLUX_TOKEN_PLANNING` or `INFLUX_TOKEN`, preferring the specific name within a
given source, so the value can be copied across without being renamed on the way.

**The sibling-checkout fallback is gone.** `influx_source.py` used to read
`../../alphaess-collector/.env` as a third resolution step, so a Mac checkout beside that
repo needed no token of its own. Removed on 2026-07-30: this repo has no business reading
another one's private file, the relative path was a guess about directory layout that held
on exactly one machine, and the coupling was silent in the way that matters — after the
token split it would have handed back the **admin** token while the correctly-scoped one sat
beside it. It was doing precisely that, unnoticed, until the fallback was removed and the
Mac's own `.env` turned out to hold nothing but `KNMI_API_KEY`.

It was also quietly supplying `ALPHAESS_SYS_SN`, so removing it widened the system filter to
`<all>` until that key was written into this repo's `.env` too. Resolution is now two steps:
the real environment, then this repo's `.env`.

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

- ~~the docker network name~~ **confirmed 2026-07-30: `alphaess-net`** (see 2, "Which
  network")
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

`config()` now resolves each key through two sources, first non-empty wins:

| # | source | who uses it |
|---|---|---|
| 1 | the real environment | docker-compose on the NAS, `plan-now.sh`, a manual export |
| 2 | **this repo's `.env`** | the portable answer; documented in `.env.example` |

An interim version kept the collector's `.env` as a third step, so the Mac needed no token of
its own. **Dropped 2026-07-30** — see "Token" in the cross-repo contract for why. Every
machine now needs its own `.env`, the dev Mac included; that is a one-line cost and it removes
a dependency on where two unrelated repos happen to sit on disk.

`.env.example` **is committed** and lists every key with both the
`INFLUX_URL=http://influxdb:8086` container form and the `INFLUX_HOST=` LAN form.

The failure message now names what is missing and every path searched, and says to pass the
variables from docker-compose when in a container:

```
InfluxDB is not configured: missing INFLUX_URL (or INFLUX_HOST) and INFLUX_TOKEN (or
  INFLUX_TOKEN_PLANNING).
  Searched: the environment, then /nonexistent/.env.
  Copy .env.example to .env and fill it in, or set the variables directly (in a
  container, pass them from docker-compose).
```

**At deployment:** `cp .env.example .env` on the NAS and fill in `INFLUX_TOKEN_PLANNING` with
the `read:alphaess` + `write:planning` token of the same name from the collector's `.env`.
`entrypoint.sh` refuses to start when neither name is set, naming both - compose's own
`${VAR:?}` guard cannot express "one of these two", which is why that check moved into the
entrypoint. That file is gitignored
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
| `INFLUX_TOKEN_PLANNING` (read `alphaess`, write `planning`) | NAS `.env`, from `.env.example` | container refuses to start, naming both accepted variable names |
| `INFLUX_URL=http://influxdb:8086` | same `.env`, or compose | falls back to the LAN IP, which hairpins or fails from inside the container |
| docker network name | `docker-compose.yml`, confirmed on the NAS | container will not start |
| `battery-data/` | outside the checkout | irreplaceable history lost — see 2b |
| `KNMI_API_KEY` | nowhere yet | nothing, until the PV-forecast work is built |

`.env.example` is committed and carries all of the above except the network name, so the NAS
side is `cp .env.example .env` plus filling in one token.

---

## 2. Container — **written, build not yet run on the NAS**

`Dockerfile`, `docker-compose.yml` and `.dockerignore` are committed. Everything below is
verified as far as it can be off the NAS; the build itself needs Docker on the NAS (Docker
Desktop is not running on the Mac, and an arm64 build would not prove anything about the
DS220+'s x86_64 CBC binary anyway).

### Dockerfile

`python:3.12-slim`, OS `tzdata`, `requirements.txt`, then the `.py` files and both shell
scripts, running as a non-root user - one adopted at startup from the data mount, not fixed at build
time (see "The UID trap" below).

**`tzdata` is installed as an OS package, not just the pip one.** They serve different
consumers: `plan-now.sh` reads the clock with the shell's `date`, which resolves `TZ`
against `/usr/share/zoneinfo`, while the pip package is visible only to Python's `zoneinfo`.
Install one and not the other and the shell and the planner sit in different timezones —
exactly the failure 1.3 exists to prevent.

Two **build-time smoke tests**, because both failures would otherwise appear at 02:05 as a
missing plan:

1. CBC solves a two-variable LP and the objective is checked, not just the status. Catches a
   solver that installs but cannot execute.
2. `ZoneInfo("Europe/Amsterdam")` resolves. Catches a slim base without tzdata, which the
   planner would survive — falling back to the system clock with a warning nobody reads.

### The `/app` vs `/data` split

Code is baked into the image at `/app`; everything written goes to the bind-mounted `/data`.
That needed a change to `plan-now.sh`, because it used to `cd` to its own directory and
everything the planner writes is CWD-relative.

It now resolves `scriptDir`, changes to **`BT_DATA_DIR`** (defaulting to `scriptDir`, so the
Mac is unaffected), and calls the Python entry points by absolute path. It also exports
`PYTHONPATH=$scriptDir`, because `advise.py` imports `influx_source` **without** the
`sys.path` guard `Marstek-planning.py` has, and that only matters once the CWD is no longer
the code directory. `solar-forecast.sh` got the same treatment.

Verified on the Mac: with `BT_DATA_DIR` set, `plans/`, `logs/`, `price_cache/`, `pv_cache/`
and `solarforecast.json` all appear under the data directory and **nothing is written into
the repo**; with it unset, behaviour is unchanged.

### `.dockerignore` is an allowlist

Deliberately `*` followed by explicit `!` rules, rather than a list of exclusions. This repo
holds a year of household load data and a `.env`; a denylist that misses one pattern bakes
either into an image layer, where it survives deleting the file and is readable with
`docker history`. Simulated against the real tree: the build context is **10 files** — the six
`.py`, `requirements.txt`, the two shell scripts and `entrypoint.sh` — with no `.env`, no CSV, no cache.

### The UID trap — designed out, not documented around

The first version of this baked `PLANNER_UID:PLANNER_GID` into the image as build args and
asked whoever deployed it to match the owner of `./data` by hand, because a bind mount
replaces the image's ownership with the host's.

That is a bad design, and the reason is worth stating: **a mismatch fails silently.**
`Marstek-planning.py` wrapped both of its cache writes in a bare `except: pass`, so an
unwritable mount meant every run refetched instead of caching, and the symptom that eventually
surfaced was forecast.solar **rate-limiting** — which reads as an API problem, not a
permissions one. A deployment step that is easy to get wrong, has no feedback when wrong, and
misreports its own failure is a step that should not exist.

Replaced by **`entrypoint.sh`**, which answers the question at runtime instead of asking it at
build time:

1. Starts as root (no `USER` in the Dockerfile — the image cannot know the right UID).
2. Reads the owner off `/data` with `stat` and becomes that user via `gosu`. Whoever owns the
   directory on the host owns the files it produces. No `.env` entry, no rebuild when it
   changes, nothing to get wrong.
3. If `/data` is owned by **root**, docker created it because it did not exist. Nobody has a
   claim on it, so the entrypoint chowns it to `PLANNER_UID` (default 1000) and says so. This
   is the only case where those variables are read at all.
4. Adds a `/etc/passwd` and `/etc/group` line for the adopted UID, so `getpwuid()` does not
   raise from inside unrelated library code.
5. **Writes an actual probe file** as the target user and **refuses to start** if it cannot,
   naming the directory, the uid, and the `chown` that fixes it. A real write, not `test -w`:
   Synology carries DSM ACLs on top of the POSIX mode, so the permission bits can say yes
   where the write still fails.

The two bare `except: pass` handlers now call `warnCacheWrite()`, which prints once per path.
Cache failure stays non-fatal — the plan is already built from the response held in memory —
but it is no longer invisible.

Net effect on deployment: `mkdir -p data` as yourself before the first run, and that is the
whole of it. Skip even that and it still works, with a line in the log explaining what it did.

### docker-compose.yml

```yaml
services:
  planner:
    build: .
    environment:
      INFLUX_URL: http://influxdb:8086
      INFLUX_TOKEN: ${INFLUX_TOKEN:-}
      INFLUX_TOKEN_PLANNING: ${INFLUX_TOKEN_PLANNING:-}
      INFLUX_ORG: ${INFLUX_ORG:-home}
      INFLUX_BUCKET: ${INFLUX_BUCKET:-alphaess}     # read: actuals
      INFLUX_PLAN_BUCKET: ${INFLUX_PLAN_BUCKET:-planning}   # write: plans
      BT_TZ: Europe/Amsterdam
      TZ: Europe/Amsterdam
      PYTHONPATH: /app
    volumes:
      - ./data:/data
    networks: [alphaess-net]

networks:
  alphaess-net:
    name: alphaess-net        # confirmed on the NAS, see below
    external: true
```

### Which network — confirmed 2026-07-30

This plan expected `alphaess-collector_alphaess-net`, on the reasoning that compose prefixes
network names with the project directory. **That was wrong.** Two networks exist on the NAS
and the live one is the *unprefixed* `alphaess-net`:

```
alphaess-collector-grafana-1        alphaess-net
alphaess-collector-awtrix-pusher-1  alphaess-net
alphaess-collector-collector-1      alphaess-net
alphaess-collector-influxdb-1       alphaess-net
```

The prefixed `alphaess-collector_alphaess-net` still exists but carries no containers — a
leftover from an earlier layout. An unprefixed name means the collector's compose file sets
`name: alphaess-net` explicitly, or the network was created by hand and referenced as
external. Either way, ours joins by that literal name. **Do not delete the empty one as part
of this work**; it belongs to the collector repo to clean up.

### `INFLUX_URL` depends on a service alias, not the container name

The container is `alphaess-collector-influxdb-1`. `http://influxdb:8086` only works because
compose registers the *service* name as a network-scoped DNS alias, and aliases are per
network rather than per project — so a container from a different compose project on the same
network resolves it too.

That is the documented behaviour, but it is an assumption this whole design rests on, and it
fails at 02:05 rather than at build time. Verify before writing the Dockerfile:

```bash
sudo docker run --rm --network alphaess-net alpine:3 getent hosts influxdb
```

If the alias is absent, fall back to the container name
(`INFLUX_URL=http://alphaess-collector-influxdb-1:8086`) and note that it then breaks whenever
the collector stack is recreated with a different scale suffix.

### MTU

Joining this network inherits whatever MTU it carries. The collector needed **1400** because
the NAS uplink drops full-size TLS handshake packets, surfacing as intermittent
`SSL: UNEXPECTED_EOF_WHILE_READING`. The planner makes the same kind of outbound HTTPS calls
to forecast.solar and EnergyZero and would hit the same failure on a default 1500-MTU network.

Still to confirm on the NAS — if `alphaess-net` is *not* capped, set it explicitly on our side:

```bash
sudo docker network inspect alphaess-net \
  --format 'mtu={{index .Options "com.docker.network.driver.mtu"}}'
```

### Working directory and data

Everything the planner writes is CWD-relative. So:

- code at **`/app`**, with `PYTHONPATH=/app` — needed because `advise.py:170` and
  `influx_to_backtest_csv.py:24` import `influx_source` **without** the `sys.path` guard
  that `Marstek-planning.py:203` has
- **`WORKDIR=/data`**, bind-mounted to `/volume1/docker/battery-planning/data`

That single mount then holds `price_cache/`, `pv_cache/`, `plans/`, `logs/`, plus the
CWD-level `entsoe-output*.txt` and `solarforecast.json`.

**The mount must be writable by the container user.** `Marstek-planning.py:935` and `:1179`
wrapped their `os.makedirs` in a bare `except: pass`, so a root-owned mount failed *silently*
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

**Copied to the NAS 2026-07-30**, to
**`/volume1/documents/battery-archive/battery-archive-20260730/`**. Mounted into the container
**read-only** at `/archive`, with `P1_CSV` pointed at it. Read-only because the one dataset
that cannot be recreated should not be writable by the thing most likely to have a bug in it.

**Correction, 2026-07-31: there were two copies on the NAS, and the container was mounting the
wrong one.** `/volume1/docker/battery-archive/` is an earlier copy from 29 July, and this
document recorded it as verified — an md5 of every file's md5, sorted, matching on both sides
at `4f73c759a2262f6e3d7b50ee93abff12` across 16,704 files. Whatever that number described, it
is not that directory today:

| | `/volume1/docker/battery-archive` | `/volume1/documents/…/battery-archive-20260730` |
|---|---|---|
| S3Export files | **16,693** | 16,700 (matches the Mac) |
| AppleDouble `._*` | **21,069** | 1 |
| size | 215 MB | 132 MB |
| `sha256sum -c MANIFEST.sha256` | no manifest | 11/11 OK |

So the mounted copy was **short 7 files** and carried 21,069 macOS xattr sidecars — the
signature of a Finder/SMB copy, not of the `tar` command written down below, which excludes
`._*`. It is also inside `/volume1/docker/`, the one place this plan says the archive must
never live, because a stack rebuild can reach it.

`docker-compose.yml` now mounts the `documents` copy, via `ARCHIVE_DIR` so the next dated
archive is a `.env` line rather than an edit. **`/volume1/docker/battery-archive` was deleted
2026-07-30**, after checking that the two files that cannot be recreated —
`backtest_input_hourly.csv` and `sparky-export-20260724/p1_elec_15min_agg.csv` — were present
at identical size and mtime in the `documents` copy and on the Mac, and that the `-nopii`
tarball had reached Google Drive. Three copies before removing the fourth, and the fourth was
the only bad one.

The lesson is narrow and worth keeping: *a checksum recorded in a document proves nothing
about a directory later.* Both copies looked fine from a listing. Only counting files against
the source found the gap.

It sits beside `battery-planning/`, never inside it: a child of the checkout is one
`.gitignore` slip away from a public fork.

Once plans and actuals are landing in InfluxDB (step 4) the collector becomes the durable
record and this only guards the pre-2026-07-17 past — but that past is the part that cannot
be re-measured by waiting.

**The NAS copy is still not a backup.** SHR/RAID 1 survives a disk dying; it does not survive
fire, theft, ransomware, or an accidental delete, which RAID mirrors faithfully. There is no
Hyper Backup job. The NAS copy is the **whole** export including `S3Export/` (132 MB); the
6.7 MB core (everything but `S3Export/`) is the subset small enough to go anywhere;
**Google Drive is the chosen third copy** (2026-07-30). A private GitHub repo was the earlier
suggestion and would also work, but Drive needs no repo hygiene for a dataset that is never
edited. The Drive copy is `battery-archive-20260730-nopii.tar.gz` (986 KB, sha256
`353a77f7e40b0158…`): the same archive minus `address.csv`, `smart_meter.csv` and `user.csv`,
which carry the meter EAN, meter number and service address. Those are 16 KB that nothing in
the project reads, and a cloud copy outlives a local delete, so they do not go up. Its
`MANIFEST.sha256` lists 8 files rather than 11 so it still verifies standalone.

Note what stripping them does **not** buy: the measurements are themselves household
occupancy data — a year of hourly load says when the house is empty. The no-PII variant is
safe for a private cloud account, not for anywhere public.

#### Copied 2026-07-30

Staged on the Mac at `~/Personal/battery-archive/battery-archive-20260730/` (6.7 MB core,
11 files, plus `MANIFEST.sha256` and a `README.md` that explains the contents, the PII in
`address.csv` / `smart_meter.csv` / `user.csv`, and the exclusions). Also
`battery-archive-20260730.tar.gz`, 986 KB, sha256 `28ab61983e824330…` — the convenience bundle
for moving the archive to a third destination.

Copied to the NAS by `tar` over SSH and verified there with `sha256sum -c MANIFEST.sha256`
— all 11 OK. `S3Export/` followed separately: 16,700 files both sides.

Landed in **`/volume1/documents/battery-archive/`**, not the `/volume1/battery-archive/`
this document suggests. Creating a new top-level shared folder needs either the DSM UI or
`sudo synoshare`, and `sudo` on Data42 prompts for a password. `documents` satisfies the two
constraints that actually matter: it is a real DSM shared folder, so a future Hyper Backup
job can select it, and it is outside `/volume1/docker/`, so a `docker compose down -v` or a
stack rebuild cannot reach it. Move it if a dedicated share is ever created.

Two gotchas worth keeping:

- **Use `COPYFILE_DISABLE=1` with macOS `tar`.** Without it, every file arrives with an
  AppleDouble `._name` sibling carrying xattrs — harmless but it inflates the file count and
  breaks a naive `find | wc -l` comparison between the two sides.
- **`du` will not match and that is fine.** `S3Export/` reads 108 MB on APFS and 126 MB on
  the NAS: 16,700 tiny files, each rounded up to a 4 K block. Compare file counts and
  checksums, never `du`.

Still to do: the offsite third copy, going to Google Drive. Until that exists, both copies are
in the same building and a fire is a total loss.

#### Getting it there: rsync does not work on DSM out of the box

Recorded because it cost an hour and the error message points nowhere near the cause.

`/usr/bin/rsync` on DSM is **setuid root** and refuses `--server` mode — the mode every
incoming transfer uses — with:

```
rsync error: rsync service is no running (code 43)
```

Local rsync on the NAS works fine, so nothing looks broken until a transfer is attempted.
Over SSH the message surfaces as a bare `Permission denied, please try again.`, which reads
as an authentication failure and sends you off checking passwords and keys. It is not.
`ssh -v` settles it: the trace says `Authenticated ... using "publickey"` and *then* the
denial arrives, so the refusal is remote and post-login.

Enabling **Control Panel → File Services → rsync** starts the daemon on 873 but did **not**
lift the refusal here. The remaining suspect is that tab's *"SSH encryption port"* field,
which reads 22 while SSH actually runs on 9922 — untested, because it was not worth risking
the SSH session mid-transfer.

`tar` over SSH sidesteps all of it, needs nothing installed, and is faster anyway — 16,690 of
the files are tiny, and rsync pays a round trip per file where tar sends one stream:

```sh
cd ~/Personal/battery-data
tar cf - --exclude '.DS_Store' --exclude '._*' . \
  | ssh data42 'cd /volume1/documents/battery-archive/battery-archive-20260730 && tar xf -'
```

No `z`: `S3Export/` is already gzipped.

Verification, run on both sides and compared as one number — stronger than an rsync dry run,
since it reads every byte unconditionally rather than trusting size and mtime:

```sh
# Mac
find . -type f ! -name '.DS_Store' ! -name '._*' -print0 \
  | xargs -0 md5 -r    | awk '{print $1, $2}' | sort | md5
# NAS
find . -type f ! -name '.DS_Store' ! -name '._*' -print0 \
  | xargs -0 md5sum    | awk '{print $1, $2}' | sort | md5sum
```

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

Then **unload and delete `~/Library/LaunchAgents/com.sandeep.battery-planner.plist`** on the
Mac:

```sh
launchctl unload ~/Library/LaunchAgents/com.sandeep.battery-planner.plist
rm ~/Library/LaunchAgents/com.sandeep.battery-planner.plist
```

This document previously said the agent "was written but never loaded, so there is nothing
to unload". That was wrong, and the error was not harmless. It stayed loaded and kept firing
on the same 3-hourly schedule as the NAS: on 2026-07-30 the 20:05 slot produced two plans a
second apart, `2026-07-30T18:05:04Z` from the NAS and `...05Z` from the Mac.

The plans were identical, so nothing looked broken — which is the problem. Consumers pick
the newest `plan_run` by sorting the tag, so the Mac won every slot by one second and the
Grafana dashboard was showing the **laptop's** plan, not the NAS's. The moment the two
diverge — a sleeping laptop, a stale checkout, different `.env` values — the dashboard
silently follows the wrong one, and the NAS deployment this whole document describes stops
being what you are actually looking at. It also doubles the forecast.solar request rate
against a shared public IP with a ~12/hour budget.

Unloaded 2026-07-30. Verify with `launchctl list | grep battery-planner` — no output is
correct.

### The second task: `scripts/report.sh`, daily

A separate DSM task rather than a tail on the planning run, so a planning failure cannot take
the report with it or the reverse. Same shape as `plan.sh` — its own PATH line, its own lock
directory, its own stale-lock timeout (30 minutes here; the report is a handful of queries).

- **General**: user `root`
- **Schedule**: Daily, **06:10**, no repeat
- **Task Settings → Run command**: `/volume1/docker/battery-planning/scripts/report.sh`

**06:10, not 00:05.** The last plan of a day is written at 23:05, and the report scores each
interval against the plan in force for it — a run just after midnight would be racing the day
it is trying to score. By 06:10 yesterday is closed on both sides: every plan for it exists,
and the collector has been writing actuals continuously since. The report is then finished
before the user is awake, which is the whole point of a day-after report.

06:10 also sits in the quietest part of the planning schedule — an hour after the 05:05 run,
two before the 08:05 one. This document first said 08:10, for landing "just after the 08:05
planning run". That had it backwards: 08:10 is the slot *most* likely to overlap a planning
run, not least. The separate locks mean an overlap would have been survivable rather than
harmful, which is exactly why the error was worth catching on reasoning instead of on a
symptom — it would never have produced one.

The date is computed in the script with `TZ=Europe/Amsterdam` and passed to `report_day.py`
explicitly, rather than letting the container resolve "yesterday" on its own. The output file
is named from the same variable, so the filename and the report can never disagree about which
day they describe. An argument re-runs a past day:

```sh
sudo /volume1/docker/battery-planning/scripts/report.sh 2026-07-31
```

Output lands in `data/reports/report_YYYYMMDD.txt`, written to a temporary name and moved into
place only when the run finishes, so a half-written report is never mistaken for a complete
one. `--write` stores the per-interval comparison as `plan_score` at the same time.

Exit 1 means no plans were stored for that day — a day the planner did not run. That belongs
in the task log rather than being swallowed, so it is passed through rather than mapped to 0.

### A trap that cost a round trip: `sudo` loses the PATH

`sudo docker compose build` on the NAS fails with `sudo: docker: command not found`. DSM keeps
docker at `/usr/local/bin/docker`, and `sudo` replaces `PATH` with a `secure_path` that does
not include `/usr/local/bin`. By hand, use the absolute path:

```sh
ssh -t data42 'cd /volume1/docker/battery-planning && sudo /usr/local/bin/docker compose build'
```

`git` is at `/usr/local/bin/git` and hits the same thing over a non-interactive SSH, which
needs `export PATH=/usr/local/bin:$PATH` first. Neither affects the scheduled tasks: DSM runs
them **as** root, so there is no `sudo` and no `secure_path`, and both scripts set their own
PATH. This is precisely the difference between the by-hand path and the scheduled one, and it
is why testing by hand can fail while the schedule works.

---

## 4. Write every plan into InfluxDB — **DONE**

*(This was Phase 3 of the older plan; it lands here because it only becomes natural once the
planner is on `alphaess-net`.)*

`influx_source.linePoint()` builds one line-protocol record and `writePoints()` POSTs them to
`/api/v2/write`, reusing `config()` — no new dependency. `writePlanToInflux()` in
`Marstek-planning.py` is called from the main loop after the solve.

Schema as given in [the contract](#bucket-planning) above, plus `cost_eur` — the optimiser
already produces it per interval and it is what makes a stored plan checkable against its own
arithmetic afterwards.

Three things worth knowing about the implementation:

- **Live runs only.** The main loop walks a date range; a backtest sweeps a year of it. The
  call is guarded by `runDate.date()==today`, so hundreds of replayed days cannot land in the
  bucket under one `plan_run` stamp. `BT_WRITE_PLAN=N` turns it off entirely.
- **Non-fatal.** By the time it runs, the plan is already printed and on disk. A failed write
  warns and says the plan is unaffected. The alternative — dying at the last step because a
  service is down — loses the advice to save the dashboard, which is backwards.
- **Every field is a float**, including whole watt-hours. InfluxDB pins a field's type on
  first write and rejects a later disagreement, so a value that is `0` on a dull day and
  `0.5` on a bright one would break the series. One type everywhere cannot collide.
- **Timestamps come from `priceList[nr][2]`, the UTC start**, not the local string beside it.
  The local one repeats itself on the October DST night.

The **400-day retention on `planning`** is already set — verified on the NAS 2026-07-30,
`everySeconds: 34560000`. Nothing to do.

**Verified end to end**, 2026-07-30 17:26 from the Mac against the live NAS InfluxDB: 124
intervals × 11 fields under a single `plan_run` tag; horizon Thu 17:00 → Fri 23:45 matching
the printed advice; terminal SoC exactly 5,635 Wh against `reserve_wh` 5,635 — the reserve
constraint is visible as a binding one in the stored data. `BT_WRITE_PLAN=N` re-run produced
a plan and no second `plan_run`. The November 2025 backtest is byte-identical before and
after the change, and attempts no write.

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

## 5. Grafana panel — **BUILT** (in `alphaess-collector`, branch `battery-plan-dashboard`)

**Cross-repo:** `grafana/alphaess-battery-plan.json` in the *collector* repo, plus one mount
line in that repo's `docker-compose.yml` beside the four existing dashboards. The
provisioning provider already picks up anything in `/var/lib/grafana/dashboards`, and the
compose entrypoint's `sed` only rewrites `${DS_ALPHAESS}`, so other `${...}` in the JSON
survives for Grafana to interpolate.

**One datasource, two buckets.** The provisioned `alphaess` datasource has `defaultBucket:
alphaess`, but every Flux query names its bucket explicitly, so it reads `planning` too — no
second datasource. Confirmed the token allows it: InfluxDB carries an authorization described
`grafana: r alphaess, r planning`.

Seven panels:

1. **Plan age** — seconds since the newest `plan_run`. Amber past 3.5 h, red past 4 h; the
   schedule is every 3 h. This is the panel that makes a dead scheduler announce itself,
   which is exactly the failure that went unnoticed on 2026-07-29.
2. **Planned benefit over horizon** — `sum(cost_eur)`. Verified equal to the "total benefit"
   line `advise.py` prints for the same run (4.8863 vs +4.89 EUR).
3. **Planned SoC at horizon end** and 4. **Terminal reserve**, side by side. Equal means the
   reserve is binding and is the only thing preventing an end-of-window dump — the defect
   this whole reserve mechanism exists to fix, made visible at a glance.
5. **Planned SoC vs actual SoC**, with the reserve as a dashed floor.
6. **Planned charge/discharge** as bars, discharge drawn negative, against the buy/sell price
   on a right-hand axis.
7. **Planned actions** — every interval the plan does something in.

### Two things deliberately not done

**The action table is per-interval, not `advise.py`'s merged blocks.** Those blocks are built
in Python from the same numbers and are not stored. Rebuilding the merge in Flux would mean
writing it twice and letting the two drift; the honest options are to keep it per-interval
now, or to store the blocks later and read them.

**Panel 5 shows only the newest plan.** Stitching "the plan that was in force at each
interval" is the harder query and belongs to [section 6](#6-the-day-after-report-plan-vs-what-actually-happened--built-report_daypy),
which is what needs it.

### `plan_run` had to become UTC first

Designing panel 5 surfaced a real bug in section 4. Picking "the newest plan" means ordering
a tag, and a tag has no order but its string. `2026-10-25T02:05+02:00` sorts **above**
`2026-10-25T02:05+01:00` while being an hour **earlier** — so on the October DST night every
panel would have shown the stale 02:05 plan until 05:05, silently, once a year.

Fixed at the source: `plan_run` is now UTC RFC3339 with `Z`, where lexicographic order is
chronological order. The dashboard *additionally* parses the tag with `time(v:)` rather than
sorting it, which is correct for both formats — the two plans written on 2026-07-30 before
the fix carry a local offset and are read correctly anyway.

### Capacity is a dashboard variable

The plan stores SoC in Wh; every percentage divides by usable capacity, which is not in
InfluxDB. `capacity_wh` is a **textbox** variable defaulting to 27900 — textbox rather than
constant, because Grafana hides constants entirely, which is the opposite of the point.

### Verified

All nine queries in the dashboard were run against the live InfluxDB before committing, with
`${capacity_wh}` and the time-range macros substituted as Grafana would. Every one returns
rows. Not yet verified: how it *looks* — that needs the stack restarted on the NAS.

---

## 6. The day-after report: plan vs what actually happened — **BUILT** (`report_day.py`)

Sections 1–5 get plans made and shown. This is the one that says whether they were any
good. Deliberately scheduled after them, because it cannot start until the `planning` bucket
holds real days.

### What it answers

Three separate questions, and they should stay separate in the output — they fail
independently and mixing them hides which one went wrong.

1. **How did the day go, against what was advised?** The battery is not executing the plan.
   Nothing is: this is an advisory planner by design, and the AlphaESS runs its own
   self-consumption logic. So the honest framing is *what the optimiser advised* versus
   *what the house and battery actually did* — in euros, at the prices that actually applied.
   The gap is the answer to "is this planner worth acting on", which is the whole reason the
   project exists.
2. **How close were the outcomes?** SoC trajectory planned vs measured, and grid import/export
   planned vs measured. This is a different question from the money one — a plan can be right
   about the shape of the day and wrong about its value, or the reverse.
3. **How good was the solar forecast?** `pv_forecast_wh` against measured PV. Daily total
   error *and* error by hour of day, because the one miss observed so far (2026-07-29, 43%
   under) was concentrated in the evening rather than spread across the day. A daily total
   alone would have hidden that.

### Which plan to judge

Not one plan per day. Eight run each day, and holding the 02:05 plan responsible for the
whole day judges it on prices it could not see — tomorrow's day-ahead is not published until
~13:00.

**Compare each interval against the plan that was in force for it**: for interval *t*, the
most recent `plan_run` at or before *t*. That is what a person acting on the advice would
have been following. The 02:05 plan's own forward view can still be scored separately as a
"how far ahead does this stay right" question, but it is a second report, not this one.

### Data

Everything needed will already be there, which is the point of doing section 4 first.

| what | where |
|---|---|
| planned SoC, charge, discharge, import, export, prices, PV and load forecast | `planning` bucket, measurement `plan`, tagged `plan_run` |
| actual SoC, PV, load, grid | `alphaess` bucket, `power_readings`, via the existing `hourlyEnergyWh()` |
| prices that actually applied | already stored on every plan point as `price_buy` / `price_sell` — no refetch, and no risk of a later price revision changing a past verdict |

No new source, no new credential. The scoped token already reads `alphaess` and writes
`planning`.

### Shape

A script in this repo — `report_day.py`, run for a date, defaulting to yesterday. Text
output first, in the register `advise.py` already uses: blocks a person can read, not a CSV.
A Grafana panel can come after, and should reuse the same numbers rather than recompute them
in Flux, so the two cannot disagree.

Worth writing the per-interval comparison back into `planning` under its own measurement
(`plan_score` or similar) so the panel is a query rather than a file read, and so a run of
bad days is visible as a trend instead of as eight text files.

### Known difficulties, stated now rather than discovered later

- **Attribution.** When the actual outcome beats the plan, it may be because the plan was
  wrong, or because the forecast it was built on was wrong. Reporting the PV forecast error
  beside the money gap is what makes that separable — a bad-money day with a good forecast is
  a planner problem; both bad is a forecast problem.
- **Nothing followed the plan**, so early reports measure the distance between AlphaESS's
  built-in behaviour and the optimiser, not the planner's execution accuracy. That is still
  the useful number, but the report must say which it is rather than implying the plan was
  attempted.
- **Saldering changes the arithmetic on 1 Jan 2027.** The scoring has to use
  `salderingApplies()` per interval, exactly as the planner does, or reports spanning the
  boundary will silently value exports wrongly.
- **A day with a gap in the actuals** — collector outage — must be reported as incomplete, not
  scored as a very quiet house. `hourlyEnergyWh()`'s existing `min_coverage` check already
  draws that line; the report needs to surface it rather than swallow it.

### What it turned out to need — built 2026-07-30

    python3 report_day.py                 # yesterday
    python3 report_day.py 2026-07-30      # a specific local date
    python3 report_day.py --write         # and store the comparison as plan_score

The three sections are as specified. Four things the spec did not anticipate:

**Actuals had to become quarter-hourly.** `hourlyEnergyWh()` was hourly and hard-coded, but
plans are quarter-hourly since the NL day-ahead moved to a 15-minute MTU. Scoring at hourly
resolution would average away exactly the short price spikes the planner exists to catch —
the effect measured at +10–14% in every winter month. So `influx_source.py` gained
`intervalEnergyWh(field, start, stop, minutes=…)`, keyed by aware local datetimes, with
`hourlyEnergyWh()` kept as a thin wrapper over it so `advise.py`, `influx_to_backtest_csv.py`
and the planner's own load profile are untouched. `intervalLastValue()` came with it, for
SoC — a state field, where averaging blurs a trajectory and the value at the interval end is
the one that means something. And `planPoints()`, because nothing could read a plan back.

**Saldering needed no code at all.** The concern was that a report spanning 1 Jan 2027 would
value exports wrongly unless it repeated `salderingApplies()` per interval. It does not have
to: `price_sell` was written onto each plan point *with the regime that applied to that
interval*, so using the stored prices settles it. This is a second reason not to refetch
prices, beyond the one already recorded — a later price revision cannot change a past verdict.

**A no-battery baseline had to be added to make the money readable.** Planned cost against
actual cost says which was cheaper, not whether either was any good. The third row —
`max(load − pv, 0)` at the same stored prices, from measured load and PV — is what turns the
section into an answer. Without it a cheap day and a good plan are indistinguishable.

**Partial windows flatter the battery, and the report has to say so.** A window that opens
full and closes empty earns money it did not create; that energy was bought before the window
opened. On the first real run — 15 scored intervals of an evening — the battery "saved"
€3.57 while SoC fell 9.04 kWh. Both true, and misleading together. The money section now
prints the SoC change across the scored window beside the euros, and says the figures are a
full answer only when the window is a whole day ending near where it started.

Also worth knowing when reading section 3: `pv_forecast_wh` is the **planning** forecast, not
raw forecast.solar. It carries the elevation calibration, `pvOverallCalibration`, and the
deliberate 0.85 `pvPlanningFactor`. A steady under-forecast is partly that factor working as
designed, which matters directly for phase 5 — fitting `pvOverallCalibration` against this
number without dividing the conservatism back out would bake the conservatism in twice.

`--write` stores measurement `plan_score` in the `planning` bucket, **untagged**. Tagging it
with the `plan_run` that was judged is the obvious thing to do and would add one series per
run for ever — the same cardinality trap the `plan` measurement already carries once, on a
2 GB NAS. One series, and which run was judged stays in the text output.

### Prerequisite

At least a few complete days in the `planning` bucket, i.e. section 4 running unattended on
the NAS. The script exists and is verified against a partial day; a full day cannot be
scored until one has been planned end to end. As of 2026-07-30 the bucket holds four
`plan_run` tags, all from that evening, so the first genuinely complete report is 2026-07-31.

Two artefacts of that first evening are visible in the output and are not bugs: 70 of 96
intervals had no plan in force, because the first plan of the day was written at 17:26 and an
interval is never judged against a plan made after it; and two of the four tags are the
duplicate Mac/NAS pair one second apart, from the launchd agent described above. The
in-force rule picks the later of the two, and they were identical, so it changes nothing.

---

## Verification

| step | check |
|---|---|
| 1 | After the bash rewrite, `plan-now.sh` still produces a plan **on the Mac**. Then blank `BT_END` and confirm the `isatty` guard raises a named error rather than hanging |
| 2 | `docker compose build` succeeds and the CBC smoke test passes. First `docker compose run --rm planner` produces a plan, and `data/pv_cache/` + `data/plans/` contain new files afterwards — this is what proves the mount is writable, which is the silent-failure case |
| TZ | Run the container at 13:30 and 14:30 local; confirm the 14:30 run reports the **longer** horizon. This is the bug that would otherwise hide indefinitely |
| 3 | Trigger the DSM task by hand; confirm a plan appears with the right local timestamp. Then let one scheduled run fire unattended and read the log |
| 4 | **Done on the Mac** (124 intervals × 11 fields, one tag; terminal SoC = `reserve_wh`). Still to check on the NAS: two runs within one hour leave two distinct `plan_run` tags, neither overwriting the other |
| 5 | All nine Flux queries verified against the live database. Still to check: restart the collector stack, open **AlphaESS Battery Plan**, confirm the panels render and the actual SoC line diverges from the planned one over a few hours |
| 6 | Run the report for a day whose plans are already stored and check the euro figures by hand against the plan text file for the same day. Then run it for a date with **no** stored plan and confirm it says so rather than reporting a zero gap |
| regression | The Domoticz guard must still report **0 calls attempted** — now from inside the container |

---

## Order, and what is blocking

```
1 (portability)  ->  2 (container)  ->  3 (DSM schedule)  ->  4 (store plans)  ->  5 (Grafana)
                                                                                      |
                                                                       6 (day-after report)
```

Steps **1–4 are done**. Step 5 is the next piece of work: plans are being made every 3 hours
and stored, and nothing yet shows them.

Step 6 needs no code from step 5 — it reads the same bucket — but is placed after it because
it also needs *days* in that bucket, which only accumulate once 4 has been running
unattended. It is the first item here that answers "is any of this worth acting on", so it
should not drift indefinitely: a week of stored plans is enough to start.

Steps 5 and 6 both touch the other repo's Grafana; 6 additionally reuses the `alphaess`
bucket for actuals, which the existing token already reads.

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
