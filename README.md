
# Battery planning and control

Plan and optimise battery charging/discharging for maximised profit using hourly or 15-minute electricity prices, with the option to include solar panel production forecast and expected power usage. Normally used to plan day-ahead but can also simulate the past (provided certain conditions are met, see below).

It takes a linear programming optimisation approach for all intervals (15-min or hour) with a known price, so normally until 24:00 today or tomorrow (if tomorrows prices are already known, normally after 13:00).

The program is now designed to drive a Marstek battery, but can easily be adapted to drive other batteries, or run standalone without interface.

# Scripts at a glance

`Marstek-planning.py` is the core optimiser and can be run directly, but day to day it is driven
by a handful of wrapper scripts. Two independent paths exist — a live path that plans for right
now, and a backtest path that replays historical data:

| Script | Purpose |
|---|---|
| `Marstek-planning.py` | The LP optimiser itself. Reads prices/PV/usage/SOC, writes a plan table (`entsoe-output<date>.txt`). See "Program internal logic" below and `docs/PLAN.md` for the output format. |
| `plan-now.sh` | **Live path.** Runs the planner for the current moment (real SOC from InfluxDB, today+tomorrow's prices, fresh PV forecast), saves the plan to `plans/plan_<date>_<hour>.txt`, and runs `advise.py` as a sanity check. Advisory only — nothing is sent to the battery. |
| `advise.py` | Turns a raw plan table into a human-readable summary: consecutive intervals collapsed into labelled blocks (buy/charge from solar/sell/cover load/spill/idle) with kWh, average price and euro value per block. Can also compare a plan against what the battery actually did, via InfluxDB (`--actuals`). |
| `report_day.py` | **Day-after report.** Scores a past day's stored plans against what actually happened, in three separate sections: money at the prices that applied, outcomes (SoC and grid), and forecast error for PV and load. Each interval is judged against the plan that was in force for it, not against one plan for the whole day. `--write` stores the comparison back as measurement `plan_score`. |
| `fit_pv_elevation.py` | **PV calibration.** Reads the stored raw forecast.solar output against measured PV and reports both calibration knobs: the overall level (`pvOverallCalibration`) and the shape against sun elevation (`pvElevationLossCurve`). Says plainly when there is not yet enough data rather than fitting anyway. |
| `solar-forecast.sh` | Runs `plan-now.sh` and prints just the forecast PV generation (Wh) for today, by hour. |
| `influx_source.py` | Shared data-access module for the InfluxDB instance fed by `alphaess-collector` — battery SOC and recent hourly load/PV. This is the live data source; run `python3 influx_source.py` on its own as a connectivity self-test. |
| `clean_backtest_csv.py` | Repairs the raw APsystems EMA hourly export (redistributes outage catch-up spikes, drops zero-load days) before it's used as backtest input. |
| `influx_to_backtest_csv.py` | Exports recent measured load/PV from InfluxDB into the same CSV shape the backtester expects — a higher-fidelity alternative for recent days. |
| `p1_to_backtest_csv.py` | Builds a backtest CSV from the Sparky P1 smart-meter export (pre-battery-installation period only, using the load = solar + delivery − return identity). |
| `run-matrix.sh` | **Backtest path.** Drives the planner across an 8-run matrix (power limit × saldering × real-vs-no-battery) over a fixed historical year, to isolate what the battery itself is worth. Needs a CSV built by one of the three scripts above. |

Live path: `solar-forecast.sh` → `plan-now.sh` → `Marstek-planning.py` → `advise.py`, with
`report_day.py` closing the loop the next day.
Backtest path: `clean_backtest_csv.py` / `influx_to_backtest_csv.py` / `p1_to_backtest_csv.py` → `run-matrix.sh` → `Marstek-planning.py` (run 8×).

Deeper detail lives in `docs/PLAN.md` (plan file column format) and `NAS-DEPLOYMENT-PLAN.md` (moving the live path onto always-on hardware).

# Main Purpose

The main purpose of the program is to plan today and tomorrow. It can be re-run at any time to re-plan the remaining period, given the actual charge level of that moment. 

It is a python program that is started from a command line with various command line options to control behaviour. You can choose from:

* -t , -v , -q : tracing, verbose or quiet to specifiy output details for debuggin
* -d , -i, -s  : full domoticz integration (both input and output), integrated for input only with output to a file, standalone with manual input and output to a file
* -p : to include PV forecast (or actual for past dates)
* -u : to include estimate power usage, based on details available in domoticz short history
* -n : netting/saldering applied, affects price for return to grid
* -b : tax included (energytax and VAT/BTW)
* -z : zero import from grid (discouraged, might not always leed to optimised results)
* -h : hourly average price, otherwise 15-minute prices
* -m : use mqtt communication to Marstek cloud to get current capacity and to set mode , instead of Marstek Venus plugin via Open API. If using mqtt, please allow for the 30 seconds intervals between mqtt commands to complete. 

Most of what varies run to run (dates, battery capacity/speed overrides, SOC, price cache
locations, backtest input, terminal reserve behaviour, ...) is controlled by `BT_*` environment
variables rather than command-line flags — see "Environment variables" below. `plan-now.sh` and
`run-matrix.sh` set these for you; run `Marstek-planning.py` directly only for a one-off or
interactive session.

It can for example be scheduled from cron, from domoticz or run manually. It is specifically designed to run at the start of each price interval (for example hour) to set the battery mode for the coming interval, but taking into accouunt all know future prices etc. 

As an example, I currently use it with the following line in the crontab:
0 * * * * /usr/bin/python3 /home/pi/hame-relay/Marstek-planning.py -d -p -u -n -b -h -m >> /home/pi/hame-relay/batteryplanning.log 2>&1

The standalone mode will interactively request user input and provide feedback on the screen and in a file. The Domoticz mode will take the input from Domoticz variables and devices, load the planning onto a Domoticz text device for display and trigger the next action from the planning and send it to the battery. The standalone mode will only produce a planning (into a file) and not trigger any action.

It has the option to include solar panel production forecast in the planning for multiple pv panels groups (with the -p command line argument). It will take location and pv panel configuration data and request the forecast from forecast.solar website.

Prices will be taken from entsoe (eu transparency site) or, if not available or complete, from the energyzero website. An API token from entsoe is required, see below. Additional kWh pricing elements can be specified, such as energy tax, supplier purchase fee, network fee, cycle costs, VAT/BTW percentage.

The -m option can be used to circumvent the Marstek open API plugin setup and communicate directly with the Marstek cloud via mqtt. The hame relay setup is required for this (https://github.com/tomquist/hame-relay docker setup without home assistant) and the MAC address of the Marstek battery needs to be provided. Make sure hame-relay is tested (for example with mosquitto_sub and mosquitto_pub commands) and working before using the mqtt option here. 

Of course battery characteristics such as current charge, maximum and minimum capacity, maximum charge-speed and discharge-speed and conversion efficiency are taken into account.

# Simulate the past (backtesting)

It can also be used to run on historic price data to simulate what could have been achieved and to evaluate return on investment for a battery system. It will simulate and optimise each day, starting at 15:00 hrs to 24:00 the next day, for the total period requested.

If the price data from entsoe.eu has been downloaded before it can be re-used from existing files, instead of requesting it again (`price_cache/`). Note during simulation of the past the price data from the entsoe website is stored in local xml files with a timestamp and not automatically removed, so some manual maintenance of the file system will then be required at some point.

For the past it can also include actual PV production and actual usage, via `BACKTEST_CSV` pointed at a 3-column CSV (`datetime,load_kwh,solar_kwh`). That CSV has to be built first; three scripts do this from different sources — pick one, or run more than one and cross-check:

* `clean_backtest_csv.py` — repairs the raw APsystems EMA hourly export: redistributes hours where an outage dumped catch-up energy into a single reading (using a modelled clear-sky weight curve), and drops whole zero-load days rather than inventing data for them. Writes `<clean>.csv` plus an `<clean>.excluded.json` sidecar recording what was dropped and why.
* `influx_to_backtest_csv.py` — exports recent measured load/PV straight from InfluxDB for a `start_date end_date` range, in the same CSV shape. Higher fidelity (30s-sampled) than the EMA export for the days InfluxDB actually covers.
* `p1_to_backtest_csv.py` — builds a CSV from the Sparky P1 smart-meter export using the identity `load = solar + delivery - return`, valid only for the period before the battery was installed (`--until`, default the day before commissioning).

Once a CSV exists, `run-matrix.sh` drives `Marstek-planning.py -s -p -u -b -h` across an 8-run
matrix (grid power limit × saldering on/off × real battery vs. ~0 baseline) over a fixed
historical year, and writes `results/summary.tsv` — the comparison against the no-battery
baseline is what isolates the battery's own contribution from the tariff/PV/usage backdrop.

# Domoticz integration mode (legacy, disabled by default)

The program can take input from Domoticz variables and devices and trigger output onto Domoticz devices — this was the original design. It is now off by default (`useDomoticz=False` near the top of `Marstek-planning.py`): every Domoticz function is still present and unmodified, but the `-d`/`-i` CLI modes refuse to run, and the two remaining call sites that would otherwise reach Domoticz (`getLocation`, `calcHourlyAvgUsage`) are cut. Standalone mode (`-s`, the live path used by `plan-now.sh`) is unaffected either way.

Set `useDomoticz=True` to restore the original behaviour. If you do, the idx numbers for the Domoticz devices will need to be adapted in the program file, as these differ for each operating environment — the first ~190 lines of the program contain all references to the Domoticz installation, the PV panel setup and the Marstek plugin, and need to be read carefully and adapted for your local installation, for example setting up the relevant user variables and adapting the IDX numbers in the python code.

Also, a confirmation email of the next planning will be sent via the Domoticz notification system. If not desired, please comment out that line.

# InfluxDB integration (current live data source)

Battery SOC and recent hourly load/PV now come from an InfluxDB instance fed by a separate
`alphaess-collector` process (`useInflux=True`, `influx_source.py`). This is what `plan-now.sh`
and `advise.py --actuals` use. Connection settings (`INFLUX_HOST`/`INFLUX_URL`, `INFLUX_TOKEN`,
`INFLUX_ORG`, `INFLUX_BUCKET`) come from the environment or are read from the collector's own
`.env` file. Run `python3 influx_source.py` on its own as a connectivity self-test — it checks
the health endpoint and prints current SOC plus a 7-day load/PV profile.

# Solar/PV panel production integration

If the -p option is added to the call of the python program, then the forecasted production of the PV panels will be included in the planning. For this the location (latitude/longitude) settings and PV configuration (Angle, Azimuth, Total MaxPeak power) needs to be defined. Hard coded, as visible in the first 60 lines.

You can define whether the PV group is connected directly to the battery (always charging the battery) or only to the home electricity net.

As the PV production is a forecast only and actual PV production will deviate, it is recommended to re-run the planning frequently, but please note that the free API of forecast.solar will only allow 10 calls per hour.

The planning will determine whether it is financially beneficial to return surplus solar energy to the grid immediately or store it for later use or return.

# How to get your ENTSOE API token for retrieving electricity prices.

To get an API token (it is free):
1. Register for an account at https://transparency.entsoe.eu/dashboard/show
2. Send an email with subject "Restful API access" and your account email address in the body.
3. After receipt of their confirmation, go into your account and generate your token.
4. Copy and paste the token to replace the xxxxxx on the line indicated in the python program where it says securitytoken="xxxxxxxxxxx" (line 52). For the Domoticz mode copy and paste the token onto the Domoticz user variable.

Energyzero will be used of the entsoe data retrieval fails.

# Environment variables

Command-line flags (`-p`, `-u`, `-n`, ...) turn features on and off; `BT_*` environment
variables set the numbers those features use, and are what let `plan-now.sh` and
`run-matrix.sh` drive the same program two very different ways without editing code. Anything
not set here falls back to the constant in the config block at the top of `Marstek-planning.py`.

| Variable | Controls |
|---|---|
| `BT_START`, `BT_END`, `BT_STARTHOUR` | Run window: start/end date (`YYYYMMDD`) and start hour |
| `BT_INITCHARGE` | Initial battery charge in Wh, or the literal `influx` to pull the live SOC from InfluxDB |
| `BT_MINSOC` | Minimum state of charge, % |
| `BT_CAP` | Override rated battery capacity, Wh |
| `BT_MAXCHG`, `BT_MAXDIS` | Override max charge/discharge speed, W |
| `BT_GRIDMAX` | Grid connection power limit, W (`0` disables the constraint) |
| `BT_RTE` | Round-trip efficiency, % |
| `BT_ETAX` | Energy tax, €/kWh |
| `BT_CYCLECOSTS` | Battery cycle cost, €/kWh discharged |
| `BT_SALDERING` | `auto` (derive net-metering regime from the date), `on`, or `off` |
| `BT_RESERVE` | `N` disables the end-of-horizon terminal SOC reserve (for A/B testing) |
| `BT_RESERVE_FLOOR` | Minimum SOC %, the reserve must not plan below this |
| `BT_ASOF_HOUR` | Simulate a past wall-clock hour, hiding prices not yet published at that hour |
| `BT_PRICE_PUBLISH_HOUR` | Hour day-ahead prices are treated as published (default 13) |
| `BACKTEST_CSV` | Path to a CSV-backed backtest input (see "Simulate the past") |
| `BT_PRICE_CACHE`, `BT_PV_CACHE` | Cache directories for price/PV forecast responses |
| `BT_ALLOW_NO_PV` | Allow planning to proceed with no PV forecast available |
| `BT_XMLAVAIL` | Whether ENTSO-E XML price files are already present locally |
| `BT_OVERWRITE` | Overwrite (`Y`) vs. append (`N`) the output plan file |

InfluxDB access (`influx_source.py`) is configured separately, via `INFLUX_HOST` or
`INFLUX_URL`, `INFLUX_TOKEN`, `INFLUX_ORG`, `INFLUX_BUCKET` — see "InfluxDB integration" above.

# Program internal logic

1) First collect all data needed for input into the planning and build the pricelist with each hour or 15-minute interval, the majority of the code.
2) Run the planning using linear optimisation across all intervals.
3) Provide the output to a file, Domoticz and the battery (see `docs/PLAN.md` for what the output file contains, and "Scripts at a glance" above for how it's consumed downstream).









