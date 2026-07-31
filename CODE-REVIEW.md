# Code review

A senior-engineer pass over the whole repo on 2026-07-31, branch
`nas-planner-and-grid-limits`. Named `CODE-REVIEW.md` rather than `CORE-REVIEW.MD` — I read
that as a typo, and `CODE-` is what a future reader will search for.

Scope: `Marstek-planning.py`, `influx_source.py`, `report_day.py`, `advise.py`,
`fit_pv_elevation.py`, `clean_backtest_csv.py`, `p1_to_backtest_csv.py`,
`influx_to_backtest_csv.py`, the four shell entry points, `Dockerfile`,
`docker-compose.yml`, `entrypoint.sh`, and the ignore files.

Everything here is either read off the code or reproduced. Where I reproduced it, the
reproduction is in the finding. Where I did not, the finding says so.

---

## Verdict first

This is not the usual "inherited script" review. The deployment work is genuinely good: the
container drops privileges off the mount owner, the `.dockerignore` is an allowlist with a
second denylist behind it, secrets have never been committed (checked the whole history),
the timezone handling is correct and *explained*, and the comments record decisions rather
than restating the code.

Three things are wrong at a level that matters:

1. **There is no test.** Not one. Zero files, no CI, no fixture. Every change to a 2,222-line
   optimiser is verified by running it and reading the output. That is the finding that
   makes the others expensive to fix, which is why the plan below starts with tests rather
   than ending with them.
2. **The one safety net has a hole, and I reproduced it.** `plan-now.sh` guards against a
   short horizon with `advise.py --min-hours 12`. An *empty* plan passes that guard and the
   whole run exits 0. See **C1**.
3. **The planner is untestable in its current shape**, because its state is ~20 module-level
   globals mutated in place. This is the single structural blocker: not a style complaint,
   a direct cause of (1).

Nothing here is a reason to stop running the planner. It works, it is being watched, and the
advisory-only design means a bad plan costs a bad suggestion. Fix in the order below.

---

## A. Testability — the blocker

### A1. No tests exist, anywhere

No `tests/`, no `pytest.ini`, no `.github/workflows`. The sibling repo `alphaess-collector`
has `tests/test_grafana_provisioning.py`; this one has nothing.

**Why it matters here specifically.** The refactorings this review recommends all touch the
arithmetic that decides money. Without a characterisation test the only way to know a
refactor was safe is to re-run the year backtest and eyeball eight numbers — which takes
minutes, catches only aggregate drift, and cannot tell you *which* interval changed.

**Fix:** the test plan at the end of this document. Build it before anything in section D.

### A2. The planner's state is global and mutated in place

`priceList`, `runDate`, `runHour`, `today`, `initialCharge`, `ratedBatteryCapacity`,
`maxChargeSpeed`, `maxDischargeSpeed`, `minBatterySOCPct`, `onewayEff`, `energyTax`,
`vatPCT`, `supplierCosts`, `networkCosts`, `cycleCosts`, `includePV`, `includeUsage`,
`includeTax`, `hourAvgPlanning`, `zeroGridCharge`, `debug`, `outputMode`, `runMode`,
`xmlAvailable`, `startDateObject`, `endDateObject`.

`getUserInput()` declares 16 of them `global` in one line (`Marstek-planning.py:479`).
`LPoptimization()` takes no arguments and returns a schedule built entirely from globals.

Consequence: you cannot call `LPoptimization()` from a test without first driving
`getUserInput()` and `buildInitialPlanningList()`, which reach the network. You cannot run
two scenarios in one process. You cannot assert on an intermediate.

**Fix (staged, not a rewrite):**
- Step 1: give `LPoptimization()` explicit parameters with defaults that read the globals.
  `def LPoptimization(priceList=None, initialCharge=None, ...)` — the live path is unchanged,
  and a test can call it with a hand-built `priceList`. This one change unlocks most of the
  test plan.
- Step 2: same for `calcTerminalReserveWh()` and `hourlyShapeFromPriceList()`.
- Step 3 (later, optional): a `PlanningConfig` dataclass. Do not start here.

### A3. `influx_source.config()` caches into a module global that nothing can reset

`influx_source.py:83,102`. `_config` is computed once and never invalidated. A test that sets
`INFLUX_URL` after any other test has touched the module gets the first test's config.

**Fix:** add `def resetConfig(): global _config; _config = None`, or accept an optional
`env` argument. Two lines, and it is the difference between config being testable and not.

---

## B. Security and secret handling

Short section, because this is the part that is in good shape. Recorded so it does not get
re-litigated.

**Confirmed clean:**
- `.env` is `-rw-------`, gitignored, and has never appeared in any commit on any branch
  (`git log --all -- .env` is empty).
- `.env.example` carries only empty keys.
- No long-lived secret appears in any tracked file.
- `influx_source._selftest()` prints the token's *length*, never its value
  (`influx_source.py:475`).
- The token travels in an `Authorization` header, never a query string, so it cannot land
  in an access log.
- `.dockerignore` is an allowlist (`*` then `!*.py`), with `.env`, `*.csv`, `plans/`,
  `logs/` re-denied underneath as a second line of defence. This is the right shape and the
  comment explains why.
- The scoped `INFLUX_TOKEN_PLANNING` (read `alphaess`, write `planning`) means the planner
  cannot corrupt measured history even with a bug. Enforced by the credential, not by
  convention.

### B1. The laptop still holds the admin token

Already in `TODO.md`, repeated here because it is the only open item in this section and a
review that omits it looks like it was not checked. `INFLUX_TOKEN` in the Mac `.env` is the
admin token: write on every bucket including `alphaess`. The NAS uses the scoped one. The
laptop is the machine most likely to be running an experiment against the live database.

### B2. Bare `except:` swallows `KeyboardInterrupt` and `SystemExit`

Sixteen occurrences in `Marstek-planning.py` (`getLocation`, `getUserVariable`,
`getPercentageDevice`, `clearTextDevice`, `setTextDevice`, `updatePowerDevice`,
`getHourlyDataFromShortHistory`, `getBatteryChargeLevel`, `updateSelectorSwitch`,
`setBatteryAction`, `loadPricesIntoFile`, `processCLarguments`, …).

A bare `except:` catches `BaseException`. Ctrl-C inside one of these becomes "ERROR: unable
to retrieve …" and the program carries on. `loadPVforecastIntoFile` already knows this — it
has an explicit `except SystemExit: raise` at `:1092` — which proves the hazard is real and
was hit once.

**Fix:** `except Exception:` everywhere. Mechanical, low risk, and worth doing in one commit
of its own so the diff is obviously safe. Most of these are on the dead Domoticz path, so
the blast radius is small; that is a reason it is cheap, not a reason to skip it.

### B3. Exception handlers reference `response` that may not exist

`getUserVariable:626`, `getLocation:607`, `setTextDevice:686`, and five siblings all do
`print("Response was : ", response.json())` inside the handler. If `requests.get` itself
raised — DNS failure, connection refused, timeout — `response` was never bound, and the
handler raises `UnboundLocalError` *from inside the error path*, replacing a clear message
with a confusing one.

Dead while `useDomoticz=False`, but these are the functions the plan says to keep for later.

### B4. Domoticz HTTP calls have no timeout and no URL escaping

`baseJSON+apiCall` with `requests.get(...)` and no `timeout=` — `getUserVariable`,
`getLocation`, `updateSelectorSwitch`, `updatePowerDevice`, `clearTextDevice`,
`getHourlyDataFromShortHistory`, and the notification `requests.get(url)` at `:925`. The
live path was fixed (`HTTP_TIMEOUT` at `:1021`, applied at `:1058`, `:1112`, `:1268`); these
were left. `setTextDevice` escapes with `urllib.parse.quote`; the notification builder at
`:922-925` interpolates `subject` and `messageBody` into a URL raw.

Same disposition as B2/B3: dead today, live if `useDomoticz` is ever flipped. Fix them when
touching that path, not before — but write it down, which is what this line is.

### B5. `setBatteryAction()` sends an email on every call, unconditionally

`Marstek-planning.py:918-925`. Below the `if mqttQuery: … else: …` branch, so *both* paths
reach it. It POSTs to a hardcoded `http://127.0.0.1:8080` regardless of `domoticzIP`, and
the result is not checked.

This is on the execution path, which is switched off by design ("no execution at this
moment"). It is listed because it is the single place in the file where a function whose
name says "set the battery" also does unrelated I/O to a hardcoded address, and because
whoever re-enables execution will not expect it.

---

## C. Correctness

### C1. An empty plan passes the horizon guard and the run exits 0 — **reproduced**

`advise.py:213-216`:

```python
rows = readPlan(path)
if not rows:
    print("no plan rows in %s" % path)
    continue          # <-- skips the --min-hours check entirely
```

The `continue` jumps over the `if minHours:` block, so `tooShort` is never set.

```
$ : > empty_plan.txt
$ python3 advise.py --min-hours 12 empty_plan.txt
no plan rows in empty_plan.txt
$ echo $?
0
```

**Why this is the top finding.** It is reachable. `buildInitialPlanningList()` gates all
data collection on `len(priceList)>0` (`:1602`, `:1644`). If ENTSOE and EnergyZero both
return nothing — a network outage, a rate limit, a stale-cache path — `priceList` is empty,
`LPoptimization()` runs with `nrIntervals=0`, CBC solves a problem with no variables and
returns `Optimal`, and `outputOptimisationResult()` writes a file containing only a header.
`plan-now.sh` renames it to `plans/plan_YYYYMMDD_HH.txt`, `advise.py` says "no plan rows",
and `scripts/plan.sh` logs `plan: done (exit 0)`.

Result: total input failure is indistinguishable from success in every artefact — the exit
code, the DSM task log, and the presence of a plan file. The one dashboard signal that would
catch it is the plan-age panel, which needs somebody looking.

**Fix (two parts, both small):**
1. `advise.py`: treat an unreadable/empty plan as a failure when `--min-hours` is given —
   set `tooShort = True` instead of bare `continue`.
2. `Marstek-planning.py`: refuse in `LPoptimization()` (or at the end of
   `buildInitialPlanningList()`) when `nrIntervals == 0`, with a named exit code, the same
   way the missing-PV-forecast guard at `:1621` already does. An optimiser asked to plan
   zero intervals should say so, not succeed.

### C2. `dropHistoryFromPricelist()` pops without checking length

`Marstek-planning.py:1526-1534`:

```python
maxDrop = 4*runHour
for interval in range(maxDrop):
    priceList.pop(0)
```

`pop(0)` on an empty list raises `IndexError`. Whenever the price fetch returns fewer
intervals than `4*runHour` — a partial EnergyZero response, a 20:05 run that got only the
back half of the day — this crashes with a traceback that names neither the cause nor the
run hour.

**Fix:** `del priceList[:min(maxDrop, len(priceList))]`, and warn when the two differ. A
clamp alone would trade a crash for the C1 silent-empty case, so the warning is the part
that matters.

### C3. Two different beliefs about when tomorrow's prices publish

`buildInitialPlanningList():1585` decides how many intervals to expect from
`currentHour >= 15`. `pricePublishHour = 13` at `:243` is what `dropUnpublishedFromPricelist()`
uses, and every comment in the repo — `plan-now.sh`, `scripts/plan.sh`, `TODO.md`, the
cadence table — says ~13:00.

So between 13:00 and 15:00 the planner expects 96 intervals when 192 are available. The
14:05 run is exactly in that window, and its entire purpose is to be the first run that sees
tomorrow. Today it is harmless: the check is `len(priceList) < expectedIntervals` and only
decides whether to *fall back* to EnergyZero, so under-expecting means "don't bother", and
ENTSOE having already returned 192 makes it moot. It becomes a real bug the moment ENTSOE
returns a partial day at 14:05 — the condition that check exists to catch.

**Fix:** one constant. `pricePublishHour` is already declared and already means this.

### C4. `influxProfileDays=7` yields 8 days — cause identified

`TODO.md` records the symptom and guesses "a `>=` where a `>` belongs". It is not that.
`influx_source.hourlyAvgProfileWh():434-436`:

```python
now   = datetime.now(LOCAL_TZ)
start = (now - timedelta(days=days)).replace(minute=0, second=0, microsecond=0)
```

`now - 7 days` at 14:20 is `2026-07-24 14:00`, and the window runs to now — so it spans
eight *calendar dates*, 07-24 through 07-31. `day_index` counts distinct dates, hence 8.

Two consequences, and the second is the one worth caring about:
- the reported day count is wrong;
- **the profile is unevenly sampled.** Hours 00:00–13:00 get 8 observations, hours
  14:00–23:00 get 7. With `weightIncrease=0.1` the oldest partial day carries weight 1.0 and
  the newest 1.7, so morning hours are averaged over a longer, differently-weighted history
  than evening hours. Small today; systematic, and it is in the load forecast the reserve
  calculation depends on.

**Fix:** snap `start` to local midnight — `.replace(hour=0, minute=0, …)` — and take
`days` whole days ending at the last complete hour. Then "7 days" is 7 days and every
hour-of-day has the same sample count.

### C5. `getSOC()` returns the wrong SoC instead of failing

`Marstek-planning.py:1561-1568`:

```python
checkRecord = len(priceList)
while int(priceList[checkRecord-1][3][11:13]) != findHour and checkRecord > 0:
    checkRecord += -1
SOC = schedule[checkRecord-1]["soc"]
```

If no interval matches `findHour`, `checkRecord` reaches 0, the indexing wraps to
`priceList[-1]`, the guard then stops the loop, and the function returns `schedule[-1]["soc"]`
— the last interval of the window — as though it were 15:00's. Silent.

Used at `main():2202` to carry SoC between days in a multi-day backtest. A day whose price
list does not reach hour 14 starts the next day from the wrong charge, and every subsequent
day inherits it.

**Fix:** search forward with an explicit sentinel and raise (or return `None`) when the hour
is absent.

### C6. `calcHourlyAvgUsage()` can raise `UnboundLocalError` in its own error path

`Marstek-planning.py:770` returns `responseResult, hourlyAvgs`, but `hourlyAvgs` is only
bound inside `if responseResult:` at `:748`. When the Domoticz fetch fails, the function
raises instead of returning `(False, …)` as its callers expect. Dead path today; exactly the
class of bug B3 describes, in a different shape.

### C7. Midnight race between the shell's date and Python's

`plan-now.sh` computes `today=$(date +%Y%m%d)` and passes it as `BT_START`.
`Marstek-planning.py:312` computes its own `today` at import. A run that crosses midnight
between those two moments has `runDate.date() != today`, which sends
`buildInitialPlanningList()` down the **historical** branch (`:1626`): it reads
`backtest_input_hourly.csv` for PV and load instead of fetching a forecast, and
`writePlanToInflux()` is skipped.

In the container the CSV is absent, so this is a `FileNotFoundError` — loud, which is the
good outcome. On the Mac the CSV is present, so it would plan today off a year-old file.

Probability is low (the schedule fires at :05). Cost is a whole silently-wrong plan. A
one-line assertion that `BT_START` equals the planner's own `today` on the live path closes
it.

### C8. `nrIntervals == 0` solves "Optimal"

Covered under C1, listed separately because the fix belongs in `LPoptimization()` and
someone reading only section D should not miss it.

---

## D. Structure, DRY, and standards

### D1. `priceList` is a list-of-lists addressed by magic index

Nine positions, addressed as `interval[3]`, `[4]`, `[6]`, `[7]` in roughly forty places
across four functions. Named constants exist — but only *inside* `LPoptimization()`
(`:1773-1777`), so the rest of the file cannot see them.

```python
priceList[intervalNr][6] += hrAvgUsage        # what is 6?
hr = int(interval[3][11:13])                  # slicing a string out of a list slot
```

Position 3 is a formatted local-time string that is then re-parsed by string slicing in at
least eight places (`hourlyShapeFromPriceList`, `calcTerminalReserveWh`,
`mergeForecastWithPricelist`, `dropUnpublishedFromPricelist`, `dropExcludedFromPricelist`,
`getSOC`, `outputOptimisationResult`, `printIntervalToFile`). Every one of those is a place
where a format change becomes a silent wrong answer rather than an error.

**Fix:** module-level `IDX_*` constants first — mechanical, greppable, no behaviour change,
and it makes every later change reviewable. A `NamedTuple` or dataclass is the right end
state; do not attempt it before the tests exist.

### D2. `/4` is spelled out in eight places

`:1447`, `:1492`, `:1520`, `:1531`, `:1803`, `:1835`, `:1836`, plus `perHour` at `:1691`.
Each is "convert a per-hour quantity to a per-quarter-hour one", each hardcodes the 4, and
each is guarded by its own `if hourAvgPlanning:`.

This is the highest-value DRY fix in the file, because the 4 is *load-bearing arithmetic on
money*. A ninth site added without the guard is a plan that is wrong by 4×, and nothing
would catch it.

**Fix:** one `intervalsPerHour()` helper (returns 1 or 4), used everywhere. Then the
quarter-hour behaviour is a single testable function.

### D3. Three near-identical linear searches, and three near-identical merges

`findForecast():1376`, `findAvgUsage():1470`, `findActual():1498` — all the same
"walk a list until the key matches" loop with a `notFound` flag, differing only in which
positions they compare. Each is called once per interval, over a list that is itself
per-interval, so the merge step is O(n²). At 192 intervals that is invisible; it is listed
as duplication, not as a performance problem.

`mergeForecastWithPricelist():1436`, `mergeUsageWithPriceList():1484`,
`mergeActualWithPricelist():1511` are the same walk-and-add with different lookups.

**Fix:** build a dict once, keyed by `(date, hour)` or `hour`, and have one merge take the
dict and a target index. Roughly 60 lines becomes roughly 20.

### D4. Battery capacity is written down three times, and can drift

| where | value | source |
|---|---|---|
| `Marstek-planning.py:128` | `ratedBatteryCapacity = 27900` | the constant block |
| `report_day.py:37` | `float(os.environ.get("BT_CAP", "27900"))` | env with a literal default |
| `advise.py:26` | `CAPACITY_WH = 27900` | literal, no override |

`plan-now.sh` deliberately does *not* set `BT_CAP`, so the planner uses its constant and
`report_day.py` uses its own identical literal. They agree today by coincidence of two
people typing the same number.

The parked upgrade — "27,900 → ~30,500 Wh" in `TODO.md` — is precisely the change that
breaks this: edit the planner and `advise.py` silently prints every SoC percentage against
the old capacity, in the output a human reads and acts on.

**Fix:** one source. The cleanest is a small `planner_config.py` holding the hardware
constants, imported by all three; `Marstek-planning.py` keeps its narrative comments there.

### D5. Four copies of the same solar-position calculation

`Marstek-planning.py:1391` (`solarElevation`, hour-resolution, uses `getLocation()`),
`fit_pv_elevation.py:58` (minute-resolution, takes lat/lon),
`clean_backtest_csv.py:46` (`solar_elevation`, hour-resolution, module-level `LAT`/`LON`),
plus the interpolation logic twice — `pvElevationCalibration():1420` and
`fit_pv_elevation._interp():248`.

`fit_pv_elevation.py` is aware of this and defends it well: `checkAgreement()` asserts its
copy agrees with the planner's at whole hours, and the docstring explains why a copy exists
(the planner's version cannot resolve below an hour). That is the right handling of a
deliberate duplicate. `clean_backtest_csv.py`'s copy has no such guard and hardcodes the
site coordinates a second time.

**Fix:** extract one `solar.py` with `elevation(lat, lon, instant)` and `interpolate(curve, x)`.
`fit_pv_elevation.checkAgreement()` then becomes unnecessary rather than merely satisfied.

### D6. Dead and redundant LP declarations

- `chargeWh`/`dischargeWh` are declared with `upBound=maxChargeSpeed` / `maxDischargeSpeed`
  (`:1784-1785`) and then constrained again per interval to `maxChargeSpeed/4` in
  quarter-hour mode (`:1835-1836`). The tighter constraint wins, so the behaviour is right —
  but the variable bound says something false, and deleting the loop constraint as
  "redundant" would quadruple the charge rate. Set the bound correctly and drop the
  constraint, or the reverse; do not keep both saying different things.
- `costsEuro = pulp.LpVariable.dicts(...)` at `:1809` is immediately overwritten by
  expressions at `:1846`. The variables are never referenced, so they never enter the
  problem. Delete the declaration.

### D7. `getUserInput()` does far more than get user input

`:477-518` reads the environment, prompts, **queries InfluxDB for live SoC**
(`getBatteryChargeLevel()` at `:503`), can `raise SystemExit(4)`, computes `onewayEff`, and
assigns the ENTSOE token and the MQTT MAC address to placeholder strings. A function that
can exit the process and open a network connection should not be named `getUserInput`.

Related: the "limited (!!) input validation" comment at `:478` is accurate. `int(_ask(...))`
on a malformed `BT_CAP` raises a bare `ValueError` with no indication of which variable was
bad.

### D8. `outputOptimisationResult()` mixes three output policies in one function

`:1885-1917` branches on `runDate == startDateObject` and
`runDate+1day == endDateObject` to decide whether to print everything, everything up to
15:00, or a 15:00-to-15:00 slice. That is a *backtest chaining* convention leaking into the
live path — `plan-now.sh` has to pass `BT_END=tomorrow` specifically to dodge it, and says
so in a comment.

The file is opened with a bare `open()` and closed at the end with no `try/finally`, so an
exception mid-write leaves the handle open and the plan file truncated.

**Fix:** `with open(...)`, and split the "which rows" decision into a function that takes the
schedule and returns rows. That function is trivially testable; the current one is not.

### D9. `advise.py`'s argument parsing is hand-rolled and order-dependent

`:200-207`. `--min-hours` is found by `rawArgs.index()` and its value taken positionally;
`--min-hours` with no value raises `IndexError`; a non-numeric value raises a bare
`ValueError`. `report_day.py` hand-rolls its own, differently (`:385-386`). Both are called
from shell scripts whose exit codes are checked.

`argparse` — which `p1_to_backtest_csv.py` already uses — costs about ten lines and removes
a class of failure from the scheduled path.

### D10. Style is consistent within files and inconsistent between them

`Marstek-planning.py` is `camelCase`, no spaces around `=`, 4-space indent with an
8-space block inside `buildInitialPlanningList()` (`:1570` onwards — the whole body is
double-indented for no reason). `influx_source.py` and `report_day.py` are PEP 8-ish with
`camelCase` public functions and `_snake_case` privates. `clean_backtest_csv.py` is
`snake_case` throughout.

This is not worth a repo-wide reformat — the churn would bury the real changes and the
upstream diff matters. But it is worth **writing down** the rule that new code follows the
style of the file it lands in, and that `Marstek-planning.py` keeps its own conventions.
Add a short `CONTRIBUTING`-style note or a section in `README.md`.

---

## E. Operations and non-functional

### E1. `report.sh` overwrites a good report with a failed one

`scripts/report.sh:78-82`. The `mv "$OUT.$$" "$OUT"` runs unconditionally, after
`|| rc=$?`. The comment says the temp-file dance exists "so a half-written report is never
mistaken for a finished one" — but a *failed* run produces a complete file full of a Docker
error, and that is what gets moved into place, destroying yesterday's good report of the
same day on a re-run.

**Fix:** move only when `rc` is 0 or 1 (1 being the documented "no plans stored"), keep the
temp file otherwise and say where it is.

### E2. `plan.sh`'s lock is not the atomic lock its comment claims

`scripts/plan.sh:24-36`:

```sh
if [ -d "$LOCK_DIR" ] && [ -z "$(find ... -mmin +$STALE_MINUTES)" ]; then exit 0; fi
rm -rf "$LOCK_DIR"
mkdir -p "$(dirname "$LOCK_DIR")"
mkdir "$LOCK_DIR"
trap 'rm -rf "$LOCK_DIR"' EXIT INT TERM
```

The comment says "mkdir is atomic, so two firings cannot both believe they won". The
unconditional `rm -rf` immediately before it removes that property: two processes that both
pass the staleness check both delete the lock, then one `mkdir` wins and the other fails
under `set -e`.

The outcome is not dangerous — the loser exits 1 before installing its trap, so it does not
delete the winner's lock — but it exits **1**, which the DSM task log records as a failed
planning run when nothing failed. Given the schedule is 3-hourly and a run takes a minute or
two, this is a theoretical race today. The fix is to make the code match the comment:
`mkdir` first, and only `rm -rf` on the branch that decided the lock was stale.

### E3. `plan-now.sh` moves a file it has not checked exists

`plan-now.sh:117`: `mv entsoe-output${today}.txt $plan`, unchecked. Coupled to C7 — if the
planner wrote a different date's filename, `mv` prints to stderr, `$plan` never appears, and
`advise.py` then fails on a missing file with a Python traceback instead of a sentence.

### E4. `solar-forecast.sh` looks like a read and is a write

It runs `plan-now.sh`, which spends a forecast.solar request (12/hour budget), writes a plan
file, writes a log, and stores a plan run in InfluxDB tagged as a real plan. A script named
"show me today's solar forecast" should not add a `plan_run` series.

**Fix:** read the newest existing plan file by default, with `--fresh` to re-plan.

### E5. `intervalEnergyWh()` issues two queries per field

`influx_source.py:358-359` — one `aggregateWindow(mean)` and one `aggregateWindow(count)`.
`report_day.collect()` calls it for four fields plus `intervalLastValue` plus `planPoints`:
ten HTTP round trips per report. Correct, and the coverage check it buys is genuinely
valuable. Noted only because a single Flux query can return both, and the report is going to
be run over longer ranges as history accumulates.

### E6. No retry on any InfluxDB call

`_query` and `writePoints` both have `timeout=30` and no retry. A single dropped packet
loses a plan's stored copy. The planner treats that as non-fatal and says so clearly, which
is the right call — but one retry with a short backoff on a 5xx would remove most of the
occurrences.

### E7. `HTTP_TIMEOUT` is defined in the planner and not shared

`Marstek-planning.py:1023` sets `(10, 30)`; `influx_source.py` hardcodes `30` in two places.
Small, but the same class as D4 — a policy expressed twice.

### E8. `requirements.txt` is pinned, unhashed, and unverified by CI

Pinning is right and the comment explaining *why* it is pinned is better than most. There is
nothing checking that the pins still install and still solve — the Dockerfile's CBC smoke
test does exactly that, but only when somebody builds. Once CI exists (Stage 0), building
the image is the natural first job.

---

## Prioritised plan

Check items off as they land. Stages are ordered by dependency, not by size: Stage 1 is
deliberately after Stage 0 because every fix in it should arrive with a test that fails
first.

### Stage 0 — make change safe (do this first)

- [x] **A3** Add `influx_source.resetConfig()` so config is testable
- [x] **A2 step 1** Give `LPoptimization()` explicit parameters defaulting to the globals
- [x] **A2 step 2** Same for `calcTerminalReserveWh()` and `hourlyShapeFromPriceList()`
- [ ] **A1** Stand up `tests/` + `pytest` + the fixture set (see the test plan below)
- [ ] **A1** Golden-file characterisation test: one fixed day in, exact plan out
- [x] **E8** CI: build the image, run the CBC smoke test, run `pytest`

### Stage 1 — correctness and silent failure

- [x] **C1a** `advise.py`: an empty plan fails `--min-hours` instead of passing
- [x] **C1b** `LPoptimization()` refuses `nrIntervals == 0` with a named exit code
- [x] **C2** `dropHistoryFromPricelist()` clamps and warns instead of `IndexError`
- [x] **C3** `buildInitialPlanningList()` uses `pricePublishHour`, not a second literal 15
      (no isolated test - see the note below the checklist)
- [x] **C4** `hourlyAvgProfileWh()` snaps to local midnight — closes the `TODO.md` item and
      the uneven-sampling bias with it
- [x] **C5** `getSOC()` raises on a missing hour instead of returning the last interval
- [x] **C7** Live path asserts `BT_START` agrees with the planner's own `today`
- [x] **E1** `report.sh` moves the report into place only on success
- [x] **E3** `plan-now.sh` checks the plan file exists before `mv`

Stage 1 done. One honest gap: **C3 has no automated test.** `buildInitialPlanningList()`
reaches ENTSOE/EnergyZero and reads `today`/`runDate` module globals with no A2-style
parametrization, so testing the one-line fix in isolation would mean mocking well past what
this fix touches. The line was read back in context to confirm it matches `pricePublishHour`
correctly; that is verification, not a test, and it is exactly the D7/D8 problem (that
function does too much) making itself felt. Worth returning to once Stage 3 decomposes it.

### Stage 2 — one source of truth

- [x] **D4** Extract hardware constants; `advise.py` and `report_day.py` stop holding their
      own copy of 27,900. **Do this before the capacity upgrade, not during it.**
      (`hardware.py`; the planned capacity upgrade can now edit one file instead of three.)
- [x] **D2** One `intervalsPerHour()` helper replaces eight hardcoded `/4`
- [x] **D1** Module-level `IDX_*` constants for `priceList` positions
- [x] **D5** One `solar.py`; `clean_backtest_csv.py` and `fit_pv_elevation.py` import it
      (bonus: `fit_pv_elevation.py`'s `checkAgreement()` now compares exactly, not within
      1° tolerance, since both sides call the same function)
- [x] **E7** Share `HTTP_TIMEOUT` (`http_config.py`)

Stage 2 done. Every fix verified behaviour-preserving before committing: the golden-file
tests stayed byte-identical throughout, and `clean_backtest_csv.py`'s D5 refactor was
additionally diffed byte-for-byte against its pre-refactor output over the real (gitignored)
backtest CSV — output and sidecar both identical. The container was rebuilt locally and the
new modules (`hardware`, `solar`, `http_config`) confirmed importable from inside it.

### Stage 3 — structure

- [x] **D3** One keyed lookup and one merge replace three of each
- [x] **D6** Fix the contradictory LP bounds; delete the dead `costsEuro` declaration
- [x] **D8** `outputOptimisationResult()` uses `with`, and row selection becomes its own
      function
- [x] **D9** `argparse` in `advise.py` and `report_day.py`
- [x] **D7** Split `getUserInput()`: reading config, and fetching live SoC, are two jobs

Stage 3 done. D6's fix was verified as a real regression risk, not a theoretical one: reverting
the corrected `upBound` and re-running its new test reproduced the quadrupled charge rate the
review warned about, confirmed the test catches it, then restored the fix. D8's row-selection
extraction was checked against a real 3-day multi-day backtest (old vs new `Marstek-planning.py`,
byte-for-byte output) that genuinely exercises all three branches of `_rowsToOutput()` — the
golden-file tests alone only cover the "everything" branch. D9 closed both concrete footguns the
review named (`--min-hours` with no value, a non-numeric value) — reproduced each crashing before
the fix and printing a clean one-line error after.

### Stage 4 — hardening the paths that are currently dead

Everything here is inert while `useDomoticz=False`. Grouped so it can be done in one sitting
if Domoticz is ever revived, and skipped entirely if it is not.

- [ ] **B2** `except Exception:` replaces sixteen bare `except:`
- [ ] **B3** Error handlers stop referencing a possibly-unbound `response`
- [ ] **B4** Timeouts and URL escaping on the Domoticz calls
- [ ] **B5** `setBatteryAction()` stops sending mail as a side effect
- [ ] **C6** `calcHourlyAvgUsage()` binds `hourlyAvgs` before returning it

### Stage 5 — nice to have

- [ ] **B1** Swap the laptop's admin token for the scoped one (already in `TODO.md`)
- [ ] **E2** `plan.sh`'s lock matches its own comment
- [ ] **E4** `solar-forecast.sh` reads instead of planning
- [ ] **E5** One Flux query for mean and count
- [ ] **E6** One retry on InfluxDB 5xx
- [ ] **D10** Write down the per-file style rule
- [ ] **A2 step 3** `PlanningConfig` dataclass — only if Stages 0-3 made it obvious

---

## Test plan

The stated goal is to "blanket the code with lots of high level tests… so that any
refactorings we did have not broken any of the core logic or the containers themselves".
That is exactly the right instinct for this codebase. The design below is shaped by one
constraint: **the tests must not need the network, the NAS, or a real InfluxDB**, or they
will not be run.

### Layer 1 — golden-file characterisation (build this first)

The point is not to assert the plan is *correct*. It is to assert the plan is *unchanged*.
That is what makes Stage 2 and 3 refactors safe.

- Freeze 2-3 days of real inputs as fixtures: an EnergyZero price JSON, a forecast.solar
  JSON per panel group, an hourly load profile. These already exist in `price_cache/` and
  `pv_cache/` — copy them into `tests/fixtures/`, and check they carry no household data
  before committing (prices and PV forecasts do not; the load profile does, so generate a
  synthetic one).
- Run the planner end to end against them with the network blocked, and store the output
  table as the golden file.
- Assert byte equality. When a refactor changes it, the diff shows which interval moved.

Cover at least: a summer day (export-heavy), a winter day (arbitrage-heavy), a day
straddling `salderingEndDate`, and one hourly (`-h`) run against one quarter-hourly run of
the same day.

### Layer 2 — the optimiser, in isolation

Unlocked by A2 step 1. Hand-build a `priceList`, call `LPoptimization()`, assert on the
schedule. Fast, exact, no I/O.

- flat prices → no cycling (arbitrage below `cycleCosts` is not worth doing)
- one cheap hour and one dear hour → charge in the first, discharge in the second
- terminal reserve respected: final SoC ≥ `calcTerminalReserveWh()`
- `BT_RESERVE=N` → the old terminal dump reappears (proves the constraint is what stops it)
- initial charge below the floor → still solves, and the first interval accepts reality
  (the `sockWh[0].lowBound` relaxation at `:1794`)
- `gridConnectionLimit` binds: charge + load > limit → import capped, plan still feasible
- `gridConnectionLimit=0` → unbounded, matching the old behaviour
- `zeroGridCharge` → every `importWh` is 0
- **`nrIntervals == 0` → refuses** (C1b; this test should fail today)
- energy balance holds for every interval: `pv + import + discharge == load + export + charge`
- SoC continuity: `soc[t] == soc[t-1] + eff*charge - discharge/eff`, to within rounding

### Layer 3 — the pure functions

No fixtures needed beyond literals.

- `calcTerminalReserveWh()`: sun-takes-over path, cheap-hour path, `reserveMaxHours` cap,
  floor dominates, never exceeds capacity
- `hoursUntilRefill()`: the midday-cheap summer case and the pre-dawn-cheap winter case,
  which is the asymmetry the whole function exists for
- `pvElevationCalibration()` / the curve interpolation: below the first breakpoint, above
  the last, exactly on a breakpoint, midway between two
- `solarElevation()`: known values at solstice and equinox for 52.5N — and the same instant
  through all copies (D5), which turns the duplication into a caught regression until it is
  removed
- `salderingApplies()`: 2026-12-31, 2027-01-01, and all three modes
- `influx_source._parseAnnotatedCsv()`: **this is the highest-value unit test in the repo.**
  The multi-block bug it fixes crashed `report_day.py` and was invisible for a day. Feed it
  a single block, two blocks with different schemas, an empty response, and a response with
  annotation lines only.
- `influx_source.linePoint()`: tag escaping (comma, space, `=`), naive vs aware timestamps,
  all-`None` fields raising
- `advise.py` `classify()` and `blocks()`: each of the seven actions, and a run of
  consecutive identical actions collapsing to one block

### Layer 4 — the InfluxDB seam

Stub `requests.post`/`requests.get`; assert on the Flux that goes out and the parsing that
comes back. No live database.

- `intervalEnergyWh()` drops intervals below `min_coverage` — the collector-outage case
- `clamp_negative` floors negatives, and is *not* applied to the sign-carrying fields
- `_windowStarts()` relabels by window start, not end (the off-by-one-window class)
- `hourlyAvgProfileWh()` returns exactly `days` days after C4
- `planPoints()` handles points with and without a `plan_run` tag (`plan` vs `plan_score`)
- `report_day.inForcePlans()`: several runs covering one interval → the newest at-or-before
  wins; an interval before every run → excluded, not scored against a later plan
- `report_day.intervalMinutes()`: 15-min points, 60-min points, a single point

### Layer 5 — the container

"…or the containers themselves" — these run in CI on every change to the Dockerfile,
`entrypoint.sh`, or `requirements.txt`.

- image builds; the CBC smoke test and the tzdata check pass (already in the Dockerfile —
  CI just has to build)
- `TZ` and `BT_TZ` both resolve to Europe/Amsterdam inside the image
- **entrypoint, root-owned mount**: docker creates `./data`, entrypoint chowns and drops to
  `PLANNER_UID`
- **entrypoint, host-owned mount**: entrypoint adopts the existing UID
- **entrypoint, unwritable mount**: refuses to start with the named error, exit 1 — the
  silent-cache-failure case the whole probe exists for
- **entrypoint, no token**: refuses, exit 1
- **entrypoint, `INFLUX_TOKEN_PLANNING` only**: starts (the either-name logic)
- `.dockerignore` holds: assert no `.env`, no `*.csv`, no `plans/`, no `logs/` in the built
  image. Run it against the image, not by reading the file — that is the check that catches
  a future `!` rule re-admitting them.

### Layer 6 — the shell entry points

`bats`, or plain `sh` scripts with a stubbed `docker` and `python3` on `PATH`.

- `plan-now.sh` picks the right energy tax per year, and warns for 2027
- `plan-now.sh` fails when neither `date -d` nor `date -v` works
- `plan-now.sh` propagates a planner failure (exit != 0) rather than continuing to `advise.py`
- `plan-now.sh` propagates an `advise.py` failure — the `tee` trap the comment describes
- `plan.sh` skips when a fresh lock is held, proceeds when the lock is stale
- `report.sh` keeps yesterday's report when today's run fails (E1)

### What not to test

Worth stating so effort does not go here: the Domoticz functions (dead by decision, and
Stage 4 changes them), the MQTT path (unused), and `run-matrix.sh` (a macOS backtest harness
that does not deploy). Assert once that Domoticz is never contacted — the existing
`scratchpad/no_domoticz.py` guard, promoted into `tests/` — and leave the rest alone.
