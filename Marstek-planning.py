########################################################################################################################################
## start of Domoticz integration definition, update these IDX numbers to match your domoticz setup #####################################

# USER VARIABLES ################
entsoeTokenIDX=12               # the IDX of the Domoticz user variable holding the API security token for transparency.entsoe.eu
## battery specs
ratedBatteryCapacityIDX=14      # the IDX of the Domoticz user variable holding the value for the rated/max battery capacity in Wh
minBatterySOCPctIDX=24          # the IDX of the Domoticz user variable holding the minimum SOC % that needs to be kept in the battery
maxBatteryChargeSpeedIDX=15     # the IDX of the Domoticz user variable holding the maximum charge speed in W
maxBatteryDischargeSpeedIDX=16  # the IDX of the Domoticz user variable holding the maximum discharge speed in W
RTEIDX=20                       # the IDX of the Domoticz user variable holding the value for the round trip conversion efficiency % of the battery system
MACaddressIDX=30                   # the IDX of the Domoticz user variable holding the MAC address of the Marstek battery (for use in mqtt)
# !!! to be added : MAC address
## price elements
energyTaxIDX=4                  # the IDX of the Domoticz user variable holding the energyTax per kWh, incl. VAT/BTW
vatIDX=13                       # the IDX of the Domoticz user variable holding the VAT/BTW percentage
supplierCostsIDX=22             # costs per kWh the electrity provider is charging (for example purchasing fee)
networkCostsIDX=25              # costs per kWh the network provider is charging (plans exist for NL after 2027, currently 0 per kWh)
cycleCostsIDX=29                # costs per kWh calculated from battery price, number of guaranteed cyles and max usable capacity (set to 0 to not have this included)

# DEVICES #######################
planningDisplayIDX=111          # the IDX number of a Domoticz text device to use for display of the planning
batterySOCIDX=372               # actual SOC pct
homeUsageIDX=178                # IDX of device holding real home usage kWh history (import + own usage PV + battery discharge) for calculation of expected load

# devices from the Marstek-Venus plugin
periodIDX=414                   # devices for holding configuration data for a manual mode activation
starttimeIDX=415
endtimeIDX=416
weekdayIDX=417
powerIDX=418
batterySwitchIDX=419            # the IDX of the Domoticz selector switch for controlling the battery system operation mode

#################################
# hard coded definition of pv panels groups (can be extended further)
# spec includes the following fields : [connection-type,angle-vs-horizontal,azimuth-vs-south,total-kWh-peak]
# "indirect" means connected to the house network, not directly to the battery
# "direct" means connected to the battery PV connections (MPPT or AC input)
# This installation is 12 x 415 Wp = 4.98 kWp, all AC-coupled (no "direct"/DC group).
# Azimuth ~0 (due south): verified against measured production, whose centroid on clear
# June days falls at 13:51 local vs a modelled solar noon of 13:45.
# Tilt values below are estimates: the seasonal signal needed to fit tilt is swamped by the
# low-sun loss corrected for in pvElevationCalibration(), so tilt could not be fitted from
# the production history. Adjust if the real roof pitch is known.
pvSpecGroup1=["indirect",35,0,3.735]    # 9 panels on the tilted roof
pvSpecGroup2=["indirect",10,0,1.245]    # 3 panels flat on the shed
pvGroups=[]
pvGroups.append(pvSpecGroup1)
pvGroups.append(pvSpecGroup2)
# repeat the above logic for each group of PV panels.

# PV forecast calibration ##########
# Measured yield is 801 kWh/kWp/yr (3987 kWh from 4.98 kWp), well under the ~900-950 a
# clear-sky model predicts for this location. The shortfall is not a flat derate: binning
# measured-vs-modelled output by solar elevation on the clearest days of each month shows
# the array performs normally when the sun is high and collapses when it is low, in every
# azimuth sector roughly equally:
#     elevation   0-10   10-20   20-30   30-40   40+
#     retained    0.24    0.55    0.69    0.87   1.00   (normalised to the high-sun plateau)
# Causes are mixed (surrounding obstructions, high angle-of-incidence reflection, inverter
# low-light behaviour, winter soiling/snow) but for forecasting only the shape matters.
# pvElevationCalibration() corrects the SHAPE; pvOverallCalibration corrects the LEVEL.
pvCalibrateForecast=True        # set False to feed raw forecast.solar values into the planning
pvOverallCalibration=1.00       # tune so that a full simulated year lands on ~801 kWh/kWp
pvPlanningFactor=0.85           # extra conservatism: over-forecast PV costs more than under-forecast
# (elevation_deg, retained_fraction) breakpoints, linearly interpolated
pvElevationLossCurve=[(0,0.20),(7.5,0.27),(12.5,0.53),(17.5,0.57),(22.5,0.67),
                      (27.5,0.71),(32.5,0.79),(37.5,0.95),(45,1.00),(90,1.00)]

# End-of-horizon reserve ##########
# Without this the optimiser values leftover stored energy at zero and therefore sells the
# battery down to the minimum SOC in the last hours of every planning window - it was
# dumping 23.8 kWh -> 2.8 kWh over the final four hours. The reserve is a boundary
# condition, not a forecast: it stops the horizon-edge dump. Because the planner re-runs
# every hour it is always recomputed with fresher data long before it would be acted on.
#
# The reserve must last from the end of the known prices until the next chance to refill,
# which is whichever comes first:
#   - the sun taking over (forecast PV exceeds forecast load), or
#   - the next cheap grid hour
#
# "Cheap" must not be assumed to mean "overnight". Measured over 2025-07..2026-06 the
# cheapest hour of the average day is midday from March to September (solar glut, cheapest
# hour 13:00-14:00) and pre-dawn only from October to February. A reserve rule that looks
# for a night-time low will misjudge two thirds of the year.
#
# So the refill hour is read from real prices wherever the planning window supplies them,
# using the average price per hour-of-day across the window. Only for hours the window does
# not cover does it fall back to typicalCheapHourByMonth, which is measured, not guessed:
# the cheapest hour of the mean day per month over that same year of EnergyZero prices.
useTerminalReserve=True         # BT_RESERVE=N overrides this, to A/B test without it
reserveFloorPct=15              # never end the window below this SOC %, whatever the sums say
reserveMarginPct=25             # safety margin on the forecast load the reserve must cover
#                                Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec
typicalCheapHourByMonth=        [  4,  4, 13, 14, 13, 14, 14, 13, 13,  3,  4,  4]
cheapQuantile=0.25              # an hour counts as a refill chance if it sits in this
#                                cheapest fraction of the window's hour-of-day price profile
reserveMaxHours=24              # cap on how far ahead the reserve is asked to stretch.
#                                Must clear a winter night-to-midday gap: in December the
#                                window can end at 23:00 with no cheap hour until 04:00 and
#                                no useful sun at all, and January load averages 23.5 kWh/day
#                                against ~25 kWh usable, so a full day on one fill is real.

# Battery and inverter ##########
# The installed hardware, in one place. These are the DEFAULTS every entry point inherits;
# BT_CAP / BT_MAXCHG / BT_MAXDIS still override for backtests and what-if runs. The values
# this file shipped with (2100 Wh, 1200 W, 800 W) described the author's 2.1 kWh Marstek and
# were wrong for this installation in every field.
#
# Charge and discharge ceilings are MEASURED, not nameplate: p99 over 11 days of 30 s
# samples from the collector. The 5 kW inverter does not reach its rating - discharge tops
# out at 4700 W (matching observation exactly) and charge at 4850 W (p99 4840, max 4868).
# Planning at 5000 W overstates the hardware by 3-6%.
#
# Upgrade path (10 kW inverter + three phase, planned):
#   inverter   nameplate   plan at        because
#   5 kW       5000 W      4850 / 4700    measured, current
#   10 kW      10000 W     ~9700 / ~9400  same 97% / 94% derate, until re-measured
# Do not simply put 10000 here on install day. Re-run the p99 against a few days of
# collector data once it is running and set the real numbers, exactly as was done for the
# 5 kW unit. A 10 kW inverter also forces three-phase: 10 kW will not pass a single-phase
# 25 A fuse (5750 W), so gridConnectionLimit below must move at the same time.
import hardware
ratedBatteryCapacity=hardware.CAPACITY_WH      # Wh, AlphaESS - see hardware.py (CODE-REVIEW.md D4):
                                                # advise.py and report_day.py read the same constant
maxChargeSpeed=4850             # W, measured p99 - see above before changing
maxDischargeSpeed=4700          # W, measured p99
# 6800 EUR all-in / (6000 cycles x 27.9 kWh x 90% DoD) = 0.0451 EUR per kWh discharged.
# Assumes the 6000-cycle warranty is the life and charges the whole install against
# throughput, so it errs high. Inherited default was 0.052, derived for the 2.1 kWh Marstek.
cycleCosts=0.0451

# Grid connection limit ##########
# maxChargeSpeed limits the BATTERY, not the meter. The energy balance makes grid import a
# derived quantity - import = load + charge + export - pv - discharge - so the actual draw
# at the meter is the charge rate PLUS whatever the house is using MINUS whatever the sun
# is giving. Force-charging at 4850 W with an 800 W house load and no sun pulls 5650 W from
# the grid, which is a number the optimiser was previously free to exceed without noticing:
# importWh and exportWh had no upper bound at all.
#
# The limit that matters is the fuse the BATTERY sits behind, not the whole-house total,
# because a single-phase battery loads one phase no matter how many the house has:
#
#   connection   battery wiring   binding limit
#   3x35A        single phase     35A x 230V  =  8050 W   <- current setup, ~3 kW headroom
#   3x35A        three phase      3 x 8050    = 24150 W
#   3x25A        single phase     25A x 230V  =  5750 W   <- WATCH THIS ONE
#   3x25A        three phase      3 x 5750    = 17250 W
#
# The 3x25A single-phase case is the one to think about before the planned upgrade this
# year: 4850 W of charging plus a 900 W house load is 5750 W, exactly the fuse. Downgrading
# amperage without moving the battery to three phase turns a slack constraint into a binding
# one, and the planner would start refusing to charge at full rate. That is the correct
# behaviour - better a slower plan than a tripped main - but it should not be a surprise.
#
# Set in watts via BT_GRIDMAX. 0 disables the constraint (the old unbounded behaviour).
gridConnectionLimit=8050        # W, single-phase battery on the current 3x35A connection
gridLimitAppliesToExport=True   # the same fuse carries export; a 4.98 kWp array cannot
#                                approach it, but the constraint is free to state

## Domoticz server
# This installation does NOT use Domoticz. Set useDomoticz=True to restore the original
# behaviour: every Domoticz function is still present and unmodified, they are simply not
# reached while this is False. The cords are cut at the three places Domoticz would still
# be contacted outside "-d"/"-i" mode (getLocation, calcHourlyAvgUsage) plus the CLI flags.
useDomoticz=False
domoticzIP="192.168.178.218"    # IP address of the Domoticz server. Can be set to 127.0.0.1 if planning is run at domoticz system itself.
domoticzPort="8080"             # Domoticz port

## InfluxDB (alphaess-collector) - the live data source replacing Domoticz.
# Supplies battery SOC and recent hourly load/PV. Connection settings come from the
# environment or the collector's own .env: see influx_source.py. Self-test with
#   python3 influx_source.py
useInflux=True
influxProfileDays=7             # days of history to average into the hourly load profile
# Store every live plan in InfluxDB as well as in the text file. BT_WRITE_PLAN=N turns it
# off. Only live runs are written; a backtest sweeps hundreds of days and would bury the
# real plans under replays of the past.
writePlansToInflux=True

# Site location, used for the PV forecast request and the solar-elevation calibration.
# With Domoticz these came from its settings; standalone they are configured here.
siteLatitude="52.5"             # degrees north
siteLongitude="5.5"             # degrees east

# all communication with domoticz devices/database is with JSON calls 
baseJSON="http://"+domoticzIP+":"+domoticzPort+"/json.htm?"   # the base string for any JSON call.
## end of Domoticz integration definition ########################################################################################


# See below for configuration data of MQTT broker !!!!!!!!!!!!!!!!!!
# MQTT Broker settings
BROKER = "192.168.178.254"      # The IP address of the broker
PORT = 1883                     # The port of the broker, normally 1883
MQTT_SUB = "hame_energy/VNSA-0/device/" # the intial part of the MQTT subscribe string, please adjust device type (here VNSA-0)
MQTT_PUB = "hame_energy/VNSA-0/App/"    # the intial part of the MQTT publish string, please adjust device type (here VNSA-0)

##################################################################################################################################

from operator import itemgetter, attrgetter
from datetime import date,datetime,timedelta,timezone
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET
import requests
import copy
import json
import time, math
import sys
import os
import csv
import urllib.parse
import pulp
import paho.mqtt.client as mqtt

import sqlite3
from sqlite3 import Error

import solar
import http_config
import app_bands

# Env overrides for settings declared in the configuration block above. They live here
# because that block is evaluated before "import os".
useTerminalReserve=os.environ.get("BT_RESERVE","Y").upper()!="N"
writePlansToInflux=os.environ.get("BT_WRITE_PLAN","Y").upper()!="N"
reserveFloorPct=int(os.environ.get("BT_RESERVE_FLOOR",str(reserveFloorPct)))

# Replaying a past moment as if it were live ("what would the planner have advised at 06:00
# on 26 July?"). The historical branch always loads a full 48h of prices, which for a
# morning run is clairvoyant: tomorrow's day-ahead is not published until early afternoon,
# so a 06:00 plan built on it is not a plan anyone could have followed. Set BT_ASOF_HOUR to
# the simulated wall-clock hour and prices beyond the run date are hidden until the
# publication hour. Unset (-1) leaves every existing run untouched.
simulateAsOfHour=int(os.environ.get("BT_ASOF_HOUR","-1"))
pricePublishHour=int(os.environ.get("BT_PRICE_PUBLISH_HOUR","13"))

# Saldering (NL net metering) ends on 1 January 2027. While it applies, an exported kWh is
# netted against an imported one and therefore earns the full retail price; afterwards
# export earns only the market price plus VAT. The difference is not cosmetic - it roughly
# halves what the battery earns, and it reverses specific advice. Emptying the battery into
# a 33 ct morning is free money under saldering and loses about 14 ct/kWh without it.
#
# The regime belongs to the interval being planned, not to the day the plan is made: a
# horizon built on 31 December 2026 runs into 1 January 2027 and must switch mid-plan.
#
# Three modes, because the backtest matrix needs to force both regimes on the same dates:
#   auto (default)  decide per interval from salderingEndDate
#   on              force net metering on   (also what the -n flag does)
#   off             force it off
salderingEndDate="2027-01-01"          # first date on which saldering no longer applies
salderingMode=os.environ.get("BT_SALDERING","auto").lower()
if salderingMode not in ("auto","on","off"):
    print("WARNING: BT_SALDERING=%s is not auto/on/off; falling back to auto"%salderingMode)
    salderingMode="auto"

def salderingApplies(localDate):
    if salderingMode=="on":
        return True
    if salderingMode=="off":
        return False
    return localDate.strftime("%Y-%m-%d")<salderingEndDate

# InfluxDB source (alphaess-collector). Optional: if the module is missing or has no
# connection settings, the planner keeps working and simply reports the missing data.
try:
    # influx_source.py sits next to this file; make that work regardless of cwd or of
    # being launched through a wrapper, otherwise the import fails and the live plan
    # silently falls back to zero expected load
    sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
    import influx_source
    influxAvailable=influx_source.configured()
    if useInflux and not influxAvailable:
        print("WARNING: useInflux is set but InfluxDB is not configured (need INFLUX_URL/INFLUX_HOST + INFLUX_TOKEN).")
        print("         Run 'python3 influx_source.py' to check.")
except ImportError as e:
    influx_source=None
    influxAvailable=False
    if useInflux:
        print("WARNING: useInflux is set but influx_source.py could not be imported : ",e)

# Wall clock ##########
# Every date and hour in this program means Europe/Amsterdam, because that is what the
# prices, the PV forecast and the meter are all denominated in. Reading the clock with a
# bare datetime.now() gets that right only when the process timezone happens to agree,
# which is true on a Dutch laptop and false in a container, where TZ is usually UTC.
#
# The consequence is not cosmetic. getPricesFromEnergyZero() decides whether tomorrow's
# day-ahead prices should exist yet from "current hour >= 15". Under UTC that test fires at
# 17:00 local, so the 14:05 run - the one whose entire purpose is to pick up the ~13:00
# price release - would plan a short horizon and say nothing about it.
#
# These return NAIVE datetimes deliberately. The rest of the file compares naive values
# throughout; converting the whole program to aware datetimes is a much larger change with
# real risk of a silent one-hour error. Attaching the correct wall clock to the existing
# naive convention fixes the bug without touching that convention.
#
# Setting TZ=Europe/Amsterdam in the container is still worth doing - it makes log
# timestamps agree with these - but the program no longer depends on it.
planningTZname=os.environ.get("BT_TZ","Europe/Amsterdam")
try:
    planningTZ=ZoneInfo(planningTZname)
except Exception as e:
    # No tzdata (a slim image without the package) - fall back to the process clock and say
    # so, rather than dying or silently planning on UTC.
    print("WARNING: timezone %s unavailable (%s); falling back to the system clock. "
          "Install tzdata, or set TZ=%s."%(planningTZname,e,planningTZname))
    planningTZ=None

def localNow():
    """Current wall-clock time in the planning timezone, as a naive datetime."""
    if planningTZ is None:
        return datetime.now()
    return datetime.now(planningTZ).replace(tzinfo=None)

def localToday():
    return localNow().date()

today=localToday()
todayString=datetime.strftime(today,'%Y%m%d')
todayLongString=datetime.strftime(today,'%Y-%m-%d')

##### MQTT functions #####

def extract_mqtt_data(data):
    global initialCharge,commandAcknowlegde,modeAcknowledge,currentMode,periodDefinition
    working_status=["sleep","standby","charging","discharging","backup","ota upgrade","bypass"]
    working_mode=["automatic","manual","trading","passive","UPS","AI"]
    offonmode=["off","on"]
    onoffmode=["on","off"]
    initialCharge=None
    commandAcknowlegde=None
    modeAcknowledge=None
    currentMode=None
    periodDefinition=None
    if data!=None:
        for pair in data.split(","):
            key, value = pair.split("=", 1)
            if debug:
                if "|" in value:
                    print("key",key,"value",value.split("|"))
                else:
                    print("key",key,"value",value)
                if key=="tot_i": print("##########","total grid input energy: ",int(value)/100," kWh")
                elif key=="tot_o": print("##########","total grid output energy: ",int(value)/100, " kWh" )
                elif key=="grd_o": print("##########","combined power (in-/out+) :",value, " W")
                elif key=="grd_t": print("##########","working status :",working_status[int(value)])
                elif key=="cel_p": print("##########","actual capacity :",int(value)/100,"kWh")
                elif key=="cel_c": print("##########","SOC :",value," %")
                elif key=="wor_m": print("##########","working mode :",working_mode[int(value)])
                elif key=="mcp_w": print("##########","max charge :",value," W")
                elif key=="mdp_w": print("##########","max discharge :",value," W")
                elif key=="pv1": print("##########","pv1 power :",int(value.split("|")[0])/10," W")
                elif key=="pv2": print("##########","pv2 power :",int(value.split("|")[0])/10," W")
                elif key=="api": print("##########","api on/off :", offonmode[int(value)])
                elif key=="bl": print("##########","bluetooth lock on/off :", onoffmode[int(value)])
                elif key=="gp" : print("##########","power in(-)/out(+) from/to grid :",value)
                elif key=="bp" : print("##########","battery power in/out :", value)
                elif key=="rp" : print("##########","inverter power usage ? ",value, " W")
                elif key=="pv" : print("##########","pv energy today : ",int(value.split("|")[0])/100," kWh")
                elif key=="fu" : print("##########","surplus feed-in : ",offonmode[int(value.split("|")[0])])
            if key=="cel_p" : initialCharge=int(value)*10
            if key=="cd" : commandAcknowlegde=int(value)
            if key=="md" : modeAcknowledge=int(value)
            if key=="wor_m" : currentMode=working_mode[int(value)]
            if key=="tim_0": periodDefinition=str(value)
        if debug:
            print("key values received : initialCharge ",initialCharge," command acknowledge ",commandAcknowlegde," mode acknowledge",modeAcknowledge," current mode ",currentMode)


# Callback when the client connects to the broker
def on_mqtt_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        if debug: print("✅ Connected to MQTT Broker!")
        client.subscribe(TOPIC_SUB)
        if debug: print(f"📡 Subscribed to topic: {TOPIC_SUB}")
    else:
        if debug: print(f"❌ Failed to connect, reason code {reason_code}")

# Callback when a message is received
def on_mqtt_message(client, userdata, msg):
    try:
        payload = msg.payload.decode("utf-8")
        if debug: print(f"📥 Received message from {msg.topic}: {payload}")
        extract_mqtt_data(payload)
    except UnicodeDecodeError:
        print("⚠ Received non-text message")

# Optional logging callback (updated signature)
def on_mqtt_log(client, userdata, level, buf):
    if debug: print(f"LOG: {buf}")

# intial setup
def mqtt_setup():
    global client,initialCharge,commandAcknowlegde,modeAcknowledge,currentMode,periodDefinition,BROKER,PORT,TOPIC_SUB,TOPIC_PUB,CLIENT_ID
    TOPIC_SUB=MQTT_SUB+MACaddress+"/ctrl"
    TOPIC_PUB=MQTT_PUB+MACaddress+"/ctrl"
    CLIENT_ID = f"mqtt-client-{int(time.time())}"
    initialCharge=None
    commandAcknowlegde=None
    modeAcknowledge=None
    currentMode=None
    periodDefinition=None
    # Create MQTT client instance (API v2)
    client = mqtt.Client(
        client_id=CLIENT_ID,
        clean_session=True,
        protocol=mqtt.MQTTv311,
        transport="tcp",
        userdata=None,
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2
    )

    # Assign callbacks
    client.on_connect = on_mqtt_connect
    client.on_message = on_mqtt_message
    # client.on_log = on_mqtt_log  # Uncomment for debug logs

def mqtt_publish(message):
        result = client.publish(TOPIC_PUB, message, qos=1)
        status = result.rc  # Updated access
        if status == mqtt.MQTT_ERR_SUCCESS:
            if debug: print(f"📤 Sent message to {TOPIC_PUB}: {message}")
        else:
            if debug: print(f"❌ Failed to send message to {TOPIC_PUB}")

def mqtt_send_receive(message,expected_response):
    global commandAcknowlegde
    commandAcknowlegde=None
    try:
        # Connect to broker
        client.connect(BROKER, PORT, keepalive=60)

        # Start network loop
        client.loop_start()

        # Publish message
        mqtt_publish(message)
        time.sleep(3)

        # Keep running and repeat message until acknowledged
        print("Listening for incoming messages... Press Ctrl+C to exit.")
        while expected_response!=commandAcknowlegde:
            if debug: print(expected_response!=commandAcknowlegde,expected_response,commandAcknowlegde)
            time.sleep(30)
            if debug: print("Listening for incoming messages... Press Ctrl+C to exit.")
            mqtt_publish(message)
            time.sleep(3)

    except KeyboardInterrupt:
        if debug: print("\n🛑 Disconnecting from broker...")

    except Exception as e:
        if debug: print(f"⚠ Error: {e}")

    finally:
        client.loop_stop()
        client.disconnect()
##### End of MQTT function

##### Start of input data collection functions

def _ask(envname, prompt, default):
    # env var wins (for non-interactive batch runs); else prompt; else default
    v = os.environ.get(envname)
    if v is not None and v != "":
        return v
    if v == "":
        # Set but empty means something upstream failed to produce a value rather than
        # choosing to omit it - the Linux "date -v+1d" case, which yields an empty BT_END
        # instead of an error. Say so; silently substituting a default here would hide a
        # broken caller behind a plan that looks fine.
        print("WARNING: %s is set but empty; using the default (%s)"%(envname,default))
        return default
    if not sys.stdin.isatty():
        # No terminal means a scheduled run: cron, a container, or output redirected to a
        # log. input() there raises EOFError at best and blocks forever at worst. Taking
        # the default is what makes the constants at the top of this file the single source
        # of truth for unattended runs, so plan-now.sh can set no hardware variables at all.
        return default
    return input(prompt) or default

def _resolveInitialCharge(initialChargeSpec,startdate,ratedBatteryCapacity):
    """The one part of getUserInput() that can reach the network and exit the process
    (CODE-REVIEW.md D7) - split out so "read configuration" and "fetch the live SoC"
    are two distinguishable jobs, not folded into one function that quietly does both.

    'influx' (case-insensitive): read the real battery SoC from InfluxDB - what a
    scheduled LIVE run needs, and the value BT_INITCHARGE=influx marks. Refuses
    (does not guess) rather than plan from a wrong starting charge. Anything else:
    a plain number, what a backtest wants - a fixed, known starting point.
    """
    if str(initialChargeSpec).strip().lower()!="influx":
        return int(initialChargeSpec)
    # BT_INITCHARGE=influx is what marks this as a LIVE run, not a backtest - only
    # a scheduled run asks for the real SOC. It is also where a midnight race would
    # bite: plan-now.sh computes BT_START from the shell's own `date` before this
    # process starts, and this file separately computes its own `today` (see the
    # "Wall clock" block) at import time. A run that straddles midnight between
    # those two moments would have BT_START one day behind `today`, which sends
    # buildInitialPlanningList() down the HISTORICAL branch instead of the live
    # one - reading BACKTEST_CSV (a year-old file on the Mac, planned on silently;
    # absent entirely in the container, so at least loud there) instead of fetching
    # a forecast, and skipping writePlanToInflux() entirely. Refusing here is the
    # same call as the SOC refusal two lines below: a live run built on the wrong
    # day is worse than one that does not run.
    if datetime.strptime(startdate,'%Y%m%d').date()!=today:
        print("ERROR: BT_INITCHARGE=influx (a live run) but BT_START=%s does not match "
              "today (%s). This is the midnight race between the shell's `date` and "
              "this process's own clock - see the comment above. Refusing to plan."
              %(startdate,todayString))
        raise SystemExit(6)
    responseResult,chargeLevel=getBatteryChargeLevel()
    if not responseResult or chargeLevel is None:
        print("ERROR: BT_INITCHARGE=influx but the current SOC could not be read. Refusing to plan.")
        raise SystemExit(4)
    initialCharge=int(round(chargeLevel))
    print("initial charge from InfluxDB : %d Wh (%.1f%% of %d Wh)"%(
        initialCharge,100.0*initialCharge/ratedBatteryCapacity,ratedBatteryCapacity))
    return initialCharge

def getUserInput():
    # get user input, with limited (!!) input validation, only used in standalone mode
    # every prompt can be pre-set via an environment variable for scripted batch runs
    global initialCharge,ratedBatteryCapacity,maxChargeSpeed,maxDischargeSpeed,minBatterySOCPct,startdate,enddate,starthour,entsoeToken,onewayEff,energyTax,vatPCT,supplierCosts,networkCosts,cycleCosts,MACaddress,gridConnectionLimit
    startdate=_ask("BT_START","Enter startdate as YYYYMMDD (default=today)   : ",todayString)
    enddate=_ask("BT_END","Enter enddate as YYYYMMDD (default=startdate+1) : ",datetime.strftime(datetime.strptime(startdate,'%Y%m%d')+timedelta(days=1),'%Y%m%d'))
    starthour=int(_ask("BT_STARTHOUR","Enter start hour as HH (default next hour)   : ",datetime.strftime(localNow()+timedelta(hours=1),'%H')))
    ratedBatteryCapacity=int(_ask("BT_CAP","Enter rated capacity in Wh (default %d) :"%ratedBatteryCapacity,ratedBatteryCapacity))
    # standalone mode normally takes a fixed starting charge, which is what a backtest wants.
    # A scheduled live run must start from the battery as it actually is, so BT_INITCHARGE=influx
    # reads the current SOC instead - see _resolveInitialCharge() for why that is its own function.
    initialChargeSpec=_ask("BT_INITCHARGE","Enter initial charge in Wh, or 'influx' (default=0) :","0")
    initialCharge=_resolveInitialCharge(initialChargeSpec,startdate,ratedBatteryCapacity)
    minBatterySOCPct=int(_ask("BT_MINSOC","Enter minimum SOC in percent (default 12) :",12))
    maxChargeSpeed=int(_ask("BT_MAXCHG","Enter max charge speed in Watt (default %d) :"%maxChargeSpeed,maxChargeSpeed))
    maxDischargeSpeed=int(_ask("BT_MAXDIS","Enter max discharge speed in Watt (default %d) :"%maxDischargeSpeed,maxDischargeSpeed))
    # the fuse the battery sits behind, in Watt; 0 disables. See gridConnectionLimit above
    # for why this is not the same number as maxChargeSpeed.
    gridConnectionLimit=int(_ask("BT_GRIDMAX","Enter grid connection limit in Watt, 0=none (default %d) :"%gridConnectionLimit,gridConnectionLimit))
    if gridConnectionLimit and gridConnectionLimit<=maxChargeSpeed:
        print("WARNING: grid limit %d W is at or below the %d W charge rate. Charging alone "
              "would fill the connection, leaving nothing for the house load."
              %(gridConnectionLimit,maxChargeSpeed))
    RTE=int(_ask("BT_RTE","Enter conversion efficiency percentage RTE (default 85) :",85))
    onewayEff=float((100-(100-RTE)/2)/100)
    entsoeToken='xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'  # paste in your own security token from entsoe.eu
    MACaddress="xxxxxxxxxxxxxxxxxx" # paste the MAC address of your battery system here.
    energyTax=float(_ask("BT_ETAX","Enter energy tax in Euro per kWh (default 0.11085) :",0.11085)) # energy tax , incl btw, Euro per kWh
    supplierCosts=0.01682  # supplier/purchasing costs per kWh incl btw
    cycleCosts=float(os.environ.get("BT_CYCLECOSTS",cycleCosts)) # default set with the battery constants at the top of this file
    vatPCT=1.21  # VAT/BTW 21%
    networkCosts=0 # network costs per kWh, future development

def getPlanningInput():
    # read initial planning data from Domoticz variables and devices (instead of user input)
    global initialCharge,ratedBatteryCapacity,maxChargeSpeed,maxDischargeSpeed,minBatterySOCPct,startdate,enddate,starthour,entsoeToken,onewayEff,energyTax,vatPCT,supplierCosts,networkCosts,cycleCosts,MACaddress

    getPlanningInputSuccess=True

    startdate=todayString
    enddate=datetime.strftime(datetime.strptime(startdate,'%Y%m%d')+timedelta(days=1),'%Y%m%d')
    starthour=int(datetime.strftime(localNow(),'%H'))  # current hour. This assumes the program is called from domoticz at the start of the hour.


    responseResult,varValue=getUserVariable(MACaddressIDX)
    if responseResult: MACaddress=varValue
    print(MACaddress)
    getPlanningInputSuccess=getPlanningInputSuccess and responseResult

    if mqttQuery: # get from Marstek cloud
        mqtt_setup()
        mqtt_send_receive("cd=01",1)
    else: # or get from Domoticz device created by Marstek Venus plugin
        responseResult,varValue=getBatteryChargeLevel() # actual charge in Wh
        if responseResult: initialCharge=float(varValue)
        getPlanningInputSuccess=getPlanningInputSuccess and responseResult

    responseResult,varValue=getUserVariable(minBatterySOCPctIDX)
    if responseResult: minBatterySOCPct=float(varValue)
    getPlanningInputSuccess=getPlanningInputSuccess and responseResult

    responseResult,varValue=getUserVariable(ratedBatteryCapacityIDX)
    if responseResult: ratedBatteryCapacity=float(varValue)
    getPlanningInputSuccess=getPlanningInputSuccess and responseResult

    responseResult,varValue=getUserVariable(maxBatteryChargeSpeedIDX)
    if responseResult: maxChargeSpeed=float(varValue)
    getPlanningInputSuccess=getPlanningInputSuccess and responseResult

    responseResult,varValue=getUserVariable(maxBatteryDischargeSpeedIDX)
    if responseResult: maxDischargeSpeed=float(varValue)
    getPlanningInputSuccess=getPlanningInputSuccess and responseResult

    responseResult,varValue=getUserVariable(RTEIDX)
    if responseResult: onewayEff=float((100-(100-int(varValue))/2)/100.0)
    getPlanningInputSuccess=getPlanningInputSuccess and responseResult

    responseResult,varValue=getUserVariable(energyTaxIDX)
    if responseResult: energyTax=float(varValue)
    getPlanningInputSuccess=getPlanningInputSuccess and responseResult

    responseResult,varValue=getUserVariable(vatIDX)
    if responseResult: vatPCT=(float(varValue)+100.0)/100.0
    getPlanningInputSuccess=getPlanningInputSuccess and responseResult

    responseResult,varValue=getUserVariable(supplierCostsIDX)
    if responseResult: supplierCosts=float(varValue)
    getPlanningInputSuccess=getPlanningInputSuccess and responseResult

    responseResult,varValue=getUserVariable(networkCostsIDX)
    if responseResult: networkCosts=float(varValue)
    getPlanningInputSuccess=getPlanningInputSuccess and responseResult

    responseResult,varValue=getUserVariable(cycleCostsIDX)
    if responseResult: cycleCosts=float(varValue)
    getPlanningInputSuccess=getPlanningInputSuccess and responseResult

    responseResult,entsoeToken=getUserVariable(entsoeTokenIDX)
    getPlanningInputSuccess=getPlanningInputSuccess and responseResult

    if getPlanningInputSuccess==False:
        print("ERROR: getting all required planning data failed.")
    return getPlanningInputSuccess

def getLocation():
    # function to get the value of a location defined in settings
    if not useDomoticz:
        # standalone: take the location from the configuration block instead of Domoticz
        return True,siteLatitude,siteLongitude
    # CODE-REVIEW.md B3: response is set to None before the try, not left unbound, so
    # the except block can safely check it - if requests.get() itself raised (DNS
    # failure, connection refused, timeout), the old code referenced an undefined
    # `response` INSIDE the error handler, replacing a clear message with a confusing
    # UnboundLocalError. B4: timeout=HTTP_TIMEOUT, same policy as the live-path calls.
    response=None
    try:
        apiCall="type=command&param=getsettings"
        response = requests.get(baseJSON+apiCall,timeout=HTTP_TIMEOUT)
        responseResult=str(response.json())
        if responseResult=="ERR":
            raise Exception
        else:
            latitude=response.json()["Location"]["Latitude"]
            longitude=response.json()["Location"]["Longitude"]
            responseResult=True
    except Exception as e:
        print("ERROR: unable to retrieve the location settings (%s)"%e)
        if response is not None:
            print("Response was : ",response.json())
        responseResult=False
        latitude=None
        longitude=None
    return responseResult,latitude,longitude

def getUserVariable(varIDX):
    # function to get the value of a user variable indicated by the varIDX number
    response=None   # CODE-REVIEW.md B3: guards the except block below, see getLocation()
    try:
        apiCall="type=command&param=getuservariable&idx="+str(varIDX)
        response = requests.get(baseJSON+apiCall,timeout=HTTP_TIMEOUT)
        responseResult=str(response.json()["status"])
        if responseResult=="ERR":
            raise Exception
        else:
            varValue=response.json()["result"][0]["Value"]
            responseResult=True
    except Exception as e:
        print("ERROR: unable to retrieve the value of user variable with IDX ",varIDX," (%s)"%e)
        if response is not None:
            print("Response was : ",response.json())
        responseResult=False
        varValue=None
    return responseResult,varValue

def getPercentageDevice(varIDX):
    # function to get the value of a percentage device indicated by the varIDX number
    response=None   # CODE-REVIEW.md B3: guards the except block below, see getLocation()
    try:
        apiCall="type=command&param=getdevices&rid="+str(varIDX)
        response = requests.get(baseJSON+apiCall,timeout=HTTP_TIMEOUT)
        responseResult=str(response.json()["status"])
        if responseResult=="ERR":
            raise Exception
        else:
            varString=response.json()["result"][0]["Data"]
            varValue=float(varString.split("%")[0])
            responseResult=True
    except Exception as e:
        print("ERROR: unable to retrieve the value of device with IDX ",varIDX," (%s)"%e)
        if response is not None:
            print("Response was : ",response.json())
        responseResult=False
        varValue=None
    return responseResult,varValue

def clearTextDevice(textIDX):
    # clears the text of a text device and then clears the log entries
    responseResult=False
    if setTextDevice(textIDX,""):  # clear the text
        try:
            apiCall="type=command&param=clearlightlog&idx="+str(textIDX)
            response=requests.get(baseJSON+apiCall,timeout=HTTP_TIMEOUT)
            responseResult=str(response.json()["status"])
            if responseResult=="ERR":
                raise Exception
            else:
                responseResult=True
        except Exception as e:
            print("ERROR: log of text device with IDX ",textIDX," failed to clear (%s)."%e)
            responseResult=False
    return responseResult

def setTextDevice(textIDX,displayText):
    # update the value of a text device and adds an entry to the device log file
    response=None   # CODE-REVIEW.md B3: guards the except block below, see getLocation()
    try:
        if len(displayText)<=200:
            urlText=urllib.parse.quote(displayText)
            apiCall="type=command&param=udevice&idx="+str(textIDX)+"&nvalue=0&svalue="+urlText
            response=requests.get(baseJSON+apiCall,timeout=HTTP_TIMEOUT)
            responseResult=str(response.json()["status"])
            if responseResult=="ERR":
                raise Exception
            else:
                responseResult=True
        else:
            print("ERROR: displayText too long (max 200 characters).")
            raise Exception
    except Exception as e:
        print("ERROR: failed to update text device with IDX ",textIDX," (%s)"%e)
        if response is not None:
            print("Response was : ",response.json())
        responseResult=False
    return responseResult


def updatePowerDevice(deviceIDX,power):
    # update an electric power device
    response=None   # CODE-REVIEW.md B3: guards the except block below, see getLocation()
    try:
        apiCall="type=command&param=udevice&idx="+str(deviceIDX)+"&nvalue=0&svalue="+str(power)
        response=requests.get(baseJSON+apiCall,timeout=HTTP_TIMEOUT)
        responseResult=str(response.json()["status"])
        if responseResult=="ERR":
            raise Exception
        else:
            responseResult=True
    except Exception as e:
        print("ERROR: failed to update power device with IDX ",deviceIDX," (%s)"%e)
        if response is not None:
            print("Response was : ",response.json())
        responseResult=False
    return responseResult


def getHourlyDataFromShortHistory(varIDX):
    # get hourly history from meter device
    response=None   # CODE-REVIEW.md B3: guards the except block below, see getLocation()
    try:
        apiCall="type=command&param=graph&sensor=counter&idx="+str(varIDX)+"&range=day"
        response = requests.get(baseJSON+apiCall,timeout=HTTP_TIMEOUT)
        responseResult=str(response.json()["status"])
        if responseResult=="ERR":
            raise Exception
        else:
            varString=response.json()["result"]
            responseResult=True
    except Exception as e:
        print("ERROR: unable to retrieve the values of device with IDX ",varIDX," (%s)"%e)
        if response is not None:
            print("Response was : ",response.json())
        responseResult=False
        varString=None
    return responseResult,varString

def calcHourlyAvgUsage(varIDX,weightIncrease):
    # calculate hourly average values for meter device
    # weightIncrease >1 gives more weight to recent usage
    if useInflux and influxAvailable:
        # average the recent hourly load recorded by alphaess-collector
        try:
            hourlyAvgs,nrDays=influx_source.hourlyAvgProfileWh(
                influx_source.FIELD_LOAD,days=influxProfileDays,weightIncrease=weightIncrease)
            if nrDays>0:
                if outputMode or debug:
                    print("load profile from InfluxDB over ",nrDays," day(s), daily total ",
                          sum(v for _,v in hourlyAvgs)," Wh")
                return True,hourlyAvgs
            print("ERROR: InfluxDB returned no load samples for the last ",influxProfileDays," days")
        except Exception as e:
            print("ERROR: cannot read load history from InfluxDB : ",e)
        return False,[]
    if not useDomoticz:
        # no Domoticz history to average. This only affects planning for TODAY; backtests
        # read measured load from the CSV instead and are unaffected.
        print("WARNING: no load history source without Domoticz - planning today with zero expected load.")
        print("         Set useDomoticz=True or useInflux=True, or supply a load forecast.")
        return False,[]
    responseResult,varString=getHourlyDataFromShortHistory(varIDX)
    # Bound before the branch, not only inside it (CODE-REVIEW.md C6) - when
    # getHourlyDataFromShortHistory() fails, responseResult is False and this used to
    # reach `return responseResult,hourlyAvgs` with hourlyAvgs never assigned,
    # raising UnboundLocalError instead of the (False, []) shape every other early
    # return in this function already uses.
    hourlyAvgs=[]
    if responseResult:
        hourlyAvgs= [[f"{hour:02d}", 0] for hour in range(24)]
        weight=1
        totalWeight=weight
        firstHour=-1
        for interval in varString:
            for key,value in interval.items():
                if key=="d":
                    hour=int(value[11:13])
                if key=="v":
                    usage=int(float(value))
            if firstHour==-1:
                firstHour=hour
                nrDays=1
            else:
                if hour==firstHour:
                    nrDays+=1
                    weight+=weightIncrease
                    totalWeight+=weight
            hourlyAvgs[hour][1]+=int(usage*weight)
        for i in range(24):
            hourlyAvgs[i][1]=int(float(hourlyAvgs[i][1]/totalWeight))
    return responseResult,hourlyAvgs

def getBatteryChargeLevel():
    # get actual current battery charge level from SOC and MAX capacity
    chargeLevel=None
    if useInflux and influxAvailable:
        # SoC as recorded by alphaess-collector
        try:
            SOCPercent=influx_source.latestSocPercent()
            if SOCPercent is not None:
                return True,float(SOCPercent/100*ratedBatteryCapacity)
            print("ERROR: no recent SOC sample in InfluxDB")
        except Exception as e:
            print("ERROR: cannot read SOC from InfluxDB : ",e)
        return False,None
    try:
        responseResult,SOCPercent=getPercentageDevice(batterySOCIDX)
        if responseResult:
            # named apart from the global on purpose: assigning ratedBatteryCapacity here
            # would make it local to this whole function, and the Influx branch above reads
            # the global before this line ever runs (UnboundLocalError)
            responseResult,domoticzRatedCapacity=getUserVariable(ratedBatteryCapacityIDX)
            if responseResult:
                chargeLevel=float(SOCPercent/100*int(domoticzRatedCapacity))
                responseResult=True
            else:
                print("ERROR: retrieving max Capacity failed")
                raise Exception
        else:
            print("ERROR: retrieving actual charge percentage failed")
            raise Exception
    except Exception:
        print("ERROR: cannot get or calculate battery charge level")
        print("Response was : ",responseResult)
        responseResult=False
    return responseResult,chargeLevel

def updateSelectorSwitch(varIDX,switchLevel):
    # update a selector switch to a switch level number
    response=None   # CODE-REVIEW.md B3: guards the except block below, see getLocation()
    try:
        if type(switchLevel)==int:
            apiCall="type=command&param=switchlight&idx="+str(varIDX)+"&switchcmd=Set%20Level&level="+str(switchLevel)
            # note : unable to check whether level is valid, even invalid level will return status OK
            response = requests.get(baseJSON+apiCall,timeout=HTTP_TIMEOUT)
            responseResult=str(response.json()["status"])
            if responseResult=="ERR":
                raise Exception
            else:
                responseResult=True
        else:
            print("ERROR: incorrect type of switch level provided")
            raise Exception
    except Exception as e:
        print("ERROR: unable to set the switch with IDX ",varIDX," to value ",switchLevel," (%s)"%e)
        if response is not None:
            print("Response was : ",response.json())
        responseResult=False
    return responseResult

def setBatteryAction(action,scheduleDateTime,power,schedule):
# interface to Marstek battery, either via plugin or mqtt
    startHr=int(scheduleDateTime[11:13])
    startMin=int(scheduleDateTime[14:15])
    currentMinute=int(localNow().minute)
    power=int(float(power/(60-currentMinute))*60)
    if power>-100 and power<100 and power!=0:
        # in the app, 100 is a minimum setting for either charge or discharge
        # so in thsi case increase power but reduce time
        timepercent=float(abs(power/100))
        if hourAvgPlanning:
            endMin=int(round(60*timepercent,0))
            endHr=startHr
        else:
            endMin=int(startMin+round(15*timepercent,0))
            endHr=startHr
        if power>0:
            power=100
        else:
            power=-100
    else:
        if hourAvgPlanning:
            endHr=startHr+1
            if endHr==24: endHr=0
            endMin=startMin
        else:
            if startMin==00 or startMin==15 or startMin==30:
                endMin=startMin+15
                endHr=startHr
            else:
                endHr=startHr+1
                endMin=0
    if power>maxDischargeSpeed: power=maxDischargeSpeed
    if power<-maxChargeSpeed: power=-maxChargeSpeed
    starttimeString=f"{startHr:02d}:{startMin:02d}"
    endtimeString=f"{endHr:02d}:{endMin:02d}"
    weekdaySchedule="1111111"
    manualPeriodID="0"
    if mqttQuery:
        if action=="AutoSelf": # use auto
            message="cd=02,md=0"
            expected_response=2
        elif action=="AI": # use auto instead
            message="cd=02,md=0"
            expected_response=2
        elif action=="Manual":
            message="cd=03,md=1,nm=0,bt="+starttimeString+",et="+endtimeString+",wk=127,vv="+str(power)+",as=1"
            expected_response=3
        elif action=="Passive": # use manual zero power and disabled instead
            message="cd=03,md=1,nm=0,bt="+starttimeString+",et="+endtimeString+",wk=127,vv="+str(power)+",as=0"
            expected_response=3
        elif action=="UPS": # use manual charge full power instead
            message="cd=03,md=1,nm=0,bt="+starttimeString+",et="+endtimeString+",wk=127,vv="+"-"+str(maxChargeSpeed)+",as=1"
            expected_response=3
        if debug: print("MQTT message ",message," expected response ",expected_response)
        if debug: print("Waiting 30 seconds for next mqtt command.")
        time.sleep(30) # make sure at least 30 seconds have passed since previous mqtt message
        mqtt_send_receive(message,expected_response)
        if debug: print("Waiting 30 seconds for next mqtt command.")
        time.sleep(30)
        mqtt_send_receive("cd=01",1) # confirm request has been processed
        if debug: print("result of set battery action via mqtt : current Charge ",initialCharge," current Mode ",currentMode," period definition ",periodDefinition)
        responseResult=True
    else:
        try:
            clearTextDevice(periodIDX)
            setTextDevice(periodIDX,manualPeriodID)
            clearTextDevice(starttimeIDX)
            setTextDevice(starttimeIDX,starttimeString)
            clearTextDevice(endtimeIDX)
            setTextDevice(endtimeIDX,endtimeString)
            clearTextDevice(weekdayIDX)
            setTextDevice(weekdayIDX,weekdaySchedule)
            updatePowerDevice(powerIDX,power)
            if action=="AutoSelf": setLevel=10
            elif action=="AI": setLevel=20
            elif action=="Manual": setLevel=30
            elif action=="Passive": setLevel=40
            elif action=="UPS": setlevel=50
            updateSelectorSwitch(batterySwitchIDX,setLevel)
            responseResult=True
        except Exception:
            print("ERROR: unable to update device for setting battery action")
            responseResult=False

    fullSchedule="<br>date_______time__pvD__pvI___use__nett_chrgD_chrg_dscg__soc__imp__exp_pr-buy_pr-sell__cost<br>"
    for nr,record in enumerate(schedule):
        fullSchedule=fullSchedule+"%16s %4d %4d %5d %5d %4d %4d %4d %4d %5d %5d %1.4f %1.4f %2.4f<br>" %(priceList[nr][IDX_TIME_LOCAL],priceList[nr][IDX_PV_DIRECT],priceList[nr][IDX_PV_INDIRECT],priceList[nr][IDX_LOAD],priceList[nr][IDX_LOAD]-priceList[nr][IDX_PV_INDIRECT]-priceList[nr][IDX_PV_DIRECT],priceList[nr][IDX_PV_DIRECT],record["charge"],record["discharge"],record["soc"],record["import"],record["export"],priceList[nr][IDX_PRICE_BUY],priceList[nr][IDX_PRICE_SELL],record["costs"])
    fullSchedule=fullSchedule.replace(' ','_')  # JSON processing removes all duplicate spaces, so use underscore to get table format

    # Send an email confirmation via Domoticz's own email setup, to confirm the action
    # that was set. CODE-REVIEW.md B5: this used to run unconditionally, even when the
    # block above failed and responseResult is False - a "success" notification for a
    # failed action is actively misleading, not just noise. Also used to hardcode
    # http://127.0.0.1:8080, ignoring domoticzIP/domoticzPort; baseJSON is what every
    # other Domoticz call in this file already uses. subject/messageBody are now
    # URL-escaped (setTextDevice() already does this for its own text), and the
    # response is checked and reported, not assigned to an unused variable.
    if responseResult:
        subject = "BATTERY: next action"+str(action)
        messageBody = "Battery set to "+str(action)+" from "+starttimeString+" to "+endtimeString+" with power "+str(power)+" ( note: <0 is charge )"
        messageBody=messageBody+fullSchedule
        apiCall="type=command&param=sendnotification"
        apiCall+="&subject="+urllib.parse.quote(subject)
        apiCall+="&body="+urllib.parse.quote(messageBody)
        try:
            response=requests.get(baseJSON+apiCall,timeout=HTTP_TIMEOUT)
            if str(response.json()["status"])=="ERR":
                print("ERROR: failed to send battery-action notification email")
        except Exception as e:
            print("ERROR: failed to send battery-action notification email (%s)"%e)

    return responseResult

_BACKTEST_CSV = os.environ.get("BACKTEST_CSV", "backtest_input_hourly.csv")
_backtest_cache = None
_backtest_excluded = None

def backtestExcludedDates():
    # dates dropped by clean_backtest_csv.py (whole days of missing load written as zero).
    # Without this the planner would still run them as zero-load/zero-solar days, which the
    # optimiser reads as a free day rather than as absent data.
    global _backtest_excluded
    if _backtest_excluded is None:
        _backtest_excluded = set()
        sidecar = _BACKTEST_CSV.rsplit(".", 1)[0] + ".excluded.json"
        try:
            with open(sidecar) as f:
                _backtest_excluded = set(json.load(f).get("excluded_dates", []))
        except (OSError, ValueError):
            pass  # no sidecar (e.g. running against the raw CSV): exclude nothing
    return _backtest_excluded

def _load_backtest_csv():
    # load the AlphaESS backtest CSV once into a dict keyed by "YYYY-MM-DD HH"
    # CSV columns: datetime (YYYY-MM-DD HH:00:00), load_kwh, solar_kwh
    # values are already per-hour deltas in kWh -> converted to Wh here (gotcha #1)
    global _backtest_cache
    if _backtest_cache is None:
        _backtest_cache = {}
        with open(_BACKTEST_CSV) as f:
            r = csv.DictReader(f)
            for row in r:
                ts = row["datetime"]                       # "YYYY-MM-DD HH:00:00"
                key = ts[0:10] + " " + ts[11:13]           # "YYYY-MM-DD HH"
                load_wh  = float(row["load_kwh"])  * 1000.0 # kWh -> Wh
                solar_wh = float(row["solar_kwh"]) * 1000.0 # kWh -> Wh
                _backtest_cache[key] = (load_wh, solar_wh)
    return _backtest_cache

def getHrValueFromBIGDB(runDate,device):
    # CSV-backed replacement for the historic SQLite query, used to re-run the past.
    # device 3   = all PV, returned as the single house/indirect group (AC-coupled QS1 array)
    # device 210 = direct-coupled PV group -> none (we are NOT DC-coupled)
    # device 22  = home usage / load
    # Values are returned directly (NO diffing) as per-hour Wh (gotcha #2).
    data = _load_backtest_csv()
    hourValueList = []
    seqnr = 1
    d = runDate
    end = runDate + timedelta(days=2)
    while d < end:
        ds = d.strftime("%Y-%m-%d")
        for h in range(24):
            key = "%s %02d" % (ds, h)
            if key not in data:
                continue
            load_wh, solar_wh = data[key]
            if device == 3:
                val = solar_wh          # all AC-coupled PV as house/indirect
            elif device == 210:
                val = 0.0               # no direct-coupled group
            elif device == 22:
                val = load_wh           # consumption
            else:
                val = 0.0
            hourValueList.append([seqnr, ds, "%02d" % h, int(round(val))])
            seqnr += 1
        d = d + timedelta(days=1)
    return hourValueList

def pvCacheFileName(groupSpec,runNow=None):
    # one cache entry per panel group per clock hour. The hour bucket is deliberate: a
    # scheduled run every 3 hours must get a fresh forecast, but a retry, a manual re-run
    # or a debugging loop inside the same hour must not spend another request. The free
    # tier allows about 12 requests per hour per IP and two panel groups exhaust it fast.
    cacheDir=os.environ.get("BT_PV_CACHE","pv_cache")
    stamp=(runNow or localNow()).strftime("%Y%m%d%H")
    key="%s_%s_%s_%s"%(groupSpec[1],groupSpec[2],groupSpec[3],stamp)
    return os.path.join(cacheDir,key.replace("/","_")+".json")

def prunePVcache(keepHours=48):
    # forecasts age out; keep a couple of days so a plan can be explained after the fact
    cacheDir=os.environ.get("BT_PV_CACHE","pv_cache")
    cutoff=(localNow()-timedelta(hours=keepHours)).strftime("%Y%m%d%H")
    try:
        for name in os.listdir(cacheDir):
            stamp=name.rsplit("_",1)[-1].split(".")[0]
            if len(stamp)==10 and stamp.isdigit() and stamp<cutoff:
                os.remove(os.path.join(cacheDir,name))
    except Exception:
        pass

# (connect, read) seconds, applied to every outbound call on the live path. Shared
# with influx_source.py via http_config.py (CODE-REVIEW.md E7) - one timeout policy,
# not two copies of the same number.
HTTP_TIMEOUT=http_config.HTTP_TIMEOUT

_cacheWriteWarned=set()

def warnCacheWrite(path,err):
    # Failing to cache is not fatal - the plan is already built from the response we hold in
    # memory. But swallowing it silently means every run refetches, and against forecast.solar's
    # ~12 requests an hour that eventually surfaces as a rate-limit refusal with no visible
    # connection to the real cause. Warn once per path so a persistent problem does not bury
    # the plan in repeats.
    if path in _cacheWriteWarned:
        return
    _cacheWriteWarned.add(path)
    print("WARNING: could not write cache file %s (%s). The plan is unaffected, but nothing "
          "will be cached, so every run refetches."%(path,err))

def loadPVforecastIntoFile(groupSpec,pvForecastFileName):
    # request the PV production forecast from forecast.solar and store in a file
    cacheFile=pvCacheFileName(groupSpec)
    if os.path.exists(cacheFile):
        try:
            with open(cacheFile,"rb") as cf, open(pvForecastFileName,"wb") as f:
                f.write(cf.read())
            if debug: print("PV forecast for group %s served from cache %s"%(groupSpec,cacheFile))
            return True
        except Exception as e:
            print("WARNING: could not read PV cache %s (%s); refetching"%(cacheFile,e))
    try:
        # url components for https feed from forecast.solar
        urlwebsite='https://api.forecast.solar'
        urldoctype='/estimate/watthours/period'
        allResponseOK=True
        responseResult,latitude,longitude=getLocation()
        allResponseOK=allResponseOK and responseResult
        pvAngle=groupSpec[1]
        pvAzimuth=groupSpec[2]
        pvMaxPeak=groupSpec[3]
        if allResponseOK:
            url=urlwebsite+urldoctype+"/"+latitude+"/"+longitude+"/"+str(pvAngle)+"/"+str(pvAzimuth)+"/"+str(pvMaxPeak)+"?full=1"
            response = requests.get(url,timeout=HTTP_TIMEOUT)
            if response.status_code == 200:
                # saving the json file
                with open(pvForecastFileName, 'wb') as f:
                    f.write(response.content)
                    fileReceived=True
                try:
                    os.makedirs(os.path.dirname(cacheFile) or ".",exist_ok=True)
                    with open(cacheFile,'wb') as cf:
                        cf.write(response.content)
                    prunePVcache()
                except Exception as e:
                    warnCacheWrite(cacheFile,e)
            else:
                # forecast.solar reports the reason in the body; the free tier allows about
                # 12 requests per hour per IP, which an hourly planner with two panel groups
                # can exhaust on its own, so say which failure this is
                detail=""
                try:
                    msg=response.json().get("message",{})
                    limit=msg.get("ratelimit",{})
                    detail=" (%s; rate limit %s/%ss, %s remaining)"%(
                        msg.get("text") or response.status_code,
                        limit.get("limit"),limit.get("period"),limit.get("remaining"))
                except Exception:
                    detail=" (HTTP %s)"%response.status_code
                print("ERROR: no PV forecast for group %s%s"%(groupSpec,detail))
                fileReceived=False
                raise Exception
        else:
            print("ERROR : required parameters for requesting PV forecast not found")
            raise Exception
    except SystemExit:
        raise
    except Exception as e:
        print("ERROR: no proper PV panel forecast file received : %s"%e)
        fileReceived=False
    return fileReceived

def loadPricesIntoFile(entsoeFileName,loadStartDate,loadEndDate):
    # request the prices from entsoe.eu and store in a file
    if entsoeToken.startswith("x"):
        # placeholder token: skip ENTSOE entirely, let the EnergyZero fallback handle it
        return False
    try:
        # url components for https feed from ENTSOE.EU
        urlwebsite='https://web-api.tp.entsoe.eu/api?'
        urltoken='securityToken='+entsoeToken
        urldoctype='&documentType=A44'
        urldomain='&in_Domain=10YNL----------L&out_Domain=10YNL----------L'
        urlperiod='&periodStart='+loadStartDate+'0000&periodEnd='+loadEndDate+'2300'
        url=urlwebsite+urltoken+urldoctype+urldomain+urlperiod
        # creating HTTP response object from given url
        if debug: print("Getting data from entsoe.eu for ",loadStartDate," to ",loadEndDate)
        if debug: print(url)
        response = requests.get(url,timeout=HTTP_TIMEOUT)
        if response.status_code == 200:
            # saving the xml file
            with open(entsoeFileName, 'wb') as f:
                f.write(response.content)
            fileReceived=True
        else:
            print("ERROR: no proper price file received")
            fileReceived=False
    except Exception:
        print("ERROR: no proper price file received")
        fileReceived=False
    return fileReceived

def parsePVforecastIntoList(groupSpec):
    # process PV forecast into a list
    pvForecastFileName="solarforecast.json"
    forecastList=[]
    if loadPVforecastIntoFile(groupSpec,pvForecastFileName):
        # create PV forecast list out of json file
        with open(pvForecastFileName, "r") as read_file:
            forecastHRS = json.load(read_file)["result"]
            firstItem=True
            seqNr=0
            for key, value in forecastHRS.items():
                if not firstItem:
                    forecastWh=int(value)
                    forecastList.append([seqNr,forecastDate,forecastHr,forecastWh]) # date and time of previous line
                    seqNr+=1
                else:
                    firstItem=False
                forecastDate=key[0:10]
                forecastHr=str(key[11:13])
            for i in forecastList:
                if debug: print("forecast ",i)
    return forecastList

# Uncalibrated forecast.solar output per interval, keyed by the interval's UTC start string,
# summed over the panel groups. Filled by mergeForecastWithPricelist() and read by
# writePlanToInflux(); see the comment there for why it is worth carrying separately.
pvForecastRawWh={}

# priceList row shape - one row per planning interval, built by parsePricesIntoList() /
# getPricesFromEnergyZero() and read by roughly forty call sites across this file.
#
# CODE-REVIEW.md D1: those reads used to dereference a bare index number - interval[3],
# priceList[nr][IDX_LOAD] - each one re-deriving "what is position 6" on its own, with no
# single place that said so. LPoptimization() already had its own named aliases
# (forecastDirectIndex etc.) for exactly this reason, but only inside that one
# function - the rest of the file still could not see them. These replace both: the
# scattered magic numbers AND LPoptimization()'s function-local versions.
IDX_SEQ=0            # sequential interval number within the plan
IDX_PRICE_KWH=1      # raw market price, EUR/kWh, before tax/VAT/saldering
IDX_TIME_UTC=2       # interval start, UTC, "YYYY-MM-DD HH:MM" - the unambiguous one
IDX_TIME_LOCAL=3     # interval start, Europe/Amsterdam, same format - repeats on the October DST night
IDX_PV_DIRECT=4      # forecast/actual Wh from DC-coupled PV (always 0 here - no such group)
IDX_PV_INDIRECT=5    # forecast/actual Wh from AC-coupled PV (both this installation's groups)
IDX_LOAD=6           # forecast/actual house load, Wh
IDX_PRICE_BUY=7      # price to pay for import, EUR/kWh, tax/VAT included
IDX_PRICE_SELL=8     # price received for export, EUR/kWh - regime depends on salderingApplies()

def parsePricesIntoList(runDate,hourAverage=False,local_tz="Europe/Amsterdam"):
    # process prices into a list, either per hour or per 15-minute interval
    loadStartDate=datetime.strftime(runDate,'%Y%m%d')
    loadEndDate=datetime.strftime(runDate+timedelta(days=1),'%Y%m%d')

    # A fresh price list means a fresh set of forecasts; in a multi-day backtest this function
    # is called once per run day, and stale keys would otherwise accumulate across them.
    global pvForecastRawWh
    pvForecastRawWh={}

    priceList = []
    quarter_times = []
    processed_times = set()
    period_counter = 1
    hour_sum = 0.0
    hour_sum_usage = 0.0
    hour_sum_return = 0.0
    hour_count = 0
    hour_start = None

    # first get and parse the entsoe prices
    if runMode=="standalone" or runMode=="integrated":
        fileNameDate=datetime.strftime(runDate,'%Y%m%d')
        entsoeFileName="entsoe"+fileNameDate+".xml"
    else:
        # runMode=="domoticz"
        entsoeFileName="entsoe.xml" # no date in filename to prevent file system filling up

    if xmlAvailable[0]!="Y" and xmlAvailable[0]!="y":
        if not loadPricesIntoFile(entsoeFileName,loadStartDate,loadEndDate):
            # placeholder ENTSOE token: expected, EnergyZero fallback will supply prices
            if not entsoeToken.startswith("x"):
                print("ERROR: Something wrong with getting price data")
            return priceList

    ns = {"ns": "urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3"}
    root = ET.parse(entsoeFileName).getroot()
    local_zone = ZoneInfo(local_tz)

    # ---- filter days derived from rundate ----
    rundate_local = runDate.astimezone(local_zone).date()
    next_day_local = rundate_local + timedelta(days=1)

    periods = root.findall(".//ns:Period", ns)
    periods_sorted = sorted(
        periods,
        key=lambda p: datetime.fromisoformat(
            p.find("ns:timeInterval/ns:start", ns).text.replace("Z", "+00:00")
        )
    )
    for period in periods_sorted:
        start_text = period.find("ns:timeInterval/ns:start", ns).text
        end_text = period.find("ns:timeInterval/ns:end", ns).text
        start_dt = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end_text.replace("Z", "+00:00"))
        resolution=period.find("ns:resolution", ns).text
        if resolution=="PT15M":
            step=timedelta(minutes=15)
        else:
            step=timedelta(minutes=60)
        interval_count = int((end_dt - start_dt) / step)
        points = {
            int(p.find("ns:position", ns).text):
            float(p.find("ns:price.amount", ns).text)
            for p in period.findall("ns:Point", ns)
        }
        prev_price = None

        for pos in range(1, interval_count + 1):
            if pos in points:
                prev_price = points[pos]
            if prev_price is None:
                continue
            start_time = start_dt + (pos - 1) * step
            if start_time in processed_times:
                continue
            processed_times.add(start_time)
            quarter_times.append(start_time)
            price_kwh = prev_price / 1000.0
            # the local date decides the saldering regime, so it is needed before the prices
            start_local = start_time.astimezone(local_zone)
            if includeTax:
                price_usage=price_kwh*vatPCT+energyTax+supplierCosts+networkCosts
                if salderingApplies(start_local):
                    price_return=price_usage
                else:
                    price_return=price_kwh*vatPCT
            else:
                price_usage=price_kwh
                price_return=price_usage

            # ---- filter: rundate and rundate + 1 ----
            if start_local.date() not in (rundate_local, next_day_local):
                continue
            if not hourAverage or resolution=="PT60M":
                priceList.append([
                    period_counter,                         # sequential nr
                    price_kwh,                              # market price
                    start_time.strftime("%Y-%m-%d %H:%M"),  # period start time UTC
                    start_local.strftime("%Y-%m-%d %H:%M"), # period staret time local
                    0,                                      # pvforecast direct
                    0,                                      # pvforecast_indirect
                    0,                                      # forecast_homeusage
                    price_usage,                            # price usage
                    price_return                           # price return
                ])
                period_counter += 1
            else:
                if hour_count == 0:
                    hour_start = start_time
                hour_sum += price_kwh
                hour_sum_usage += price_usage
                hour_sum_return += price_return
                hour_count += 1
                if hour_count == 4:
                    hour_local = hour_start.astimezone(local_zone)
                    if hour_local.date() in (rundate_local, next_day_local):
                        priceList.append([
                            period_counter,
                            hour_sum / 4,
                            hour_start.strftime("%Y-%m-%d %H:%M"),
                            hour_local.strftime("%Y-%m-%d %H:%M"),
                            0,
                            0,
                            0,
                            hour_sum_usage / 4,
                            hour_sum_return / 4
                        ])
                        period_counter += 1
                    hour_sum = 0.0
                    hour_sum_usage=0.0
                    hour_sum_return=0.0
                    hour_count = 0

    return priceList

def getPricesFromEnergyZero(runDate,hourAvgPlanning,local_tz="Europe/Amsterdam"):
    # get prices from energyzero if entsoe not available or not complete
    loadStartDate=datetime.strftime(runDate,'%d-%m-%Y')
    utc = ZoneInfo("UTC")
    local_zone = ZoneInfo(local_tz)
        # ---- filter days derived from rundate ----
    rundate_local = runDate.astimezone(local_zone).date()
    next_day_local = rundate_local + timedelta(days=1)

    result= []
    # energyzero will return requested date and day before and day after
    if hourAvgPlanning or runDate<datetime.strptime("20251001","%Y%m%d"): # it can provide qtr prices even on last days of september, but we want hourly prices still
        url = "https://public.api.energyzero.nl/public/v1/prices?date="+loadStartDate+"&interval=INTERVAL_HOUR&energyType=ENERGY_TYPE_ELECTRICITY"
    else:
        url = "https://public.api.energyzero.nl/public/v1/prices?date="+loadStartDate+"&interval=INTERVAL_QUARTER&energyType=ENERGY_TYPE_ELECTRICITY"

    # cache raw EnergyZero JSON per date+interval so repeated backtest runs don't refetch.
    # A live run's rundate is today (or later): its cache entry can have been written before
    # tomorrow's day-ahead auction published, and would then never be refreshed, permanently
    # starving the plan of tomorrow's prices. Only historical (rundate < today) entries are
    # stable enough to trust from disk; today/future always refetch and overwrite.
    cacheDir=os.environ.get("BT_PRICE_CACHE","price_cache")
    cacheKey=loadStartDate.replace("-","")+("_h" if (hourAvgPlanning or runDate<datetime.strptime("20251001","%Y%m%d")) else "_q")
    cacheFile=os.path.join(cacheDir,cacheKey+".json")
    isHistorical=rundate_local<today
    responseText=None
    if isHistorical and os.path.exists(cacheFile):
        with open(cacheFile) as cf:
            responseText=cf.read()
    else:
        response=requests.get(url,timeout=HTTP_TIMEOUT)
        if response.status_code == 200:
            responseText=response.text
            try:
                os.makedirs(cacheDir,exist_ok=True)
                with open(cacheFile,"w") as cf:
                    cf.write(responseText)
            except Exception as e:
                warnCacheWrite(cacheFile,e)
        elif os.path.exists(cacheFile):
            # refetch failed (network hiccup): fall back to whatever was cached rather than
            # returning nothing
            with open(cacheFile) as cf:
                responseText=cf.read()

    if responseText is not None:
        basePrices=json.loads(responseText)
        period_counter=1
        for entry in basePrices.get("base", []):
            # Parse UTC timestamp
            start_utc = datetime.fromisoformat(entry["start"].replace("Z", "+00:00")).replace(tzinfo=utc)
            # Convert to local timezone
            start_local = start_utc.astimezone(local_zone)

            price_kwh = float(entry["price"]["value"])

            if includeTax:
                price_usage=price_kwh*vatPCT+energyTax+supplierCosts+networkCosts
                if salderingApplies(start_local):
                    price_return=price_usage
                else:
                    price_return=price_kwh*vatPCT #!!! to be done: include supplier and network costs?
            else:
                price_usage=price_kwh
                price_return=price_usage

            if start_local.date() in (rundate_local, next_day_local):
                result.append([
                    period_counter,                         # sequential nr
                    price_kwh,                              # market price
                    start_utc.strftime("%Y-%m-%d %H:%M"),  # period start time UTC
                    start_local.strftime("%Y-%m-%d %H:%M"), # period staret time local
                    0,                                      # pvforecast direct
                    0,                                      # pvforecast_indirect
                    0,                                      # forecast_homeusage
                    price_usage,                            # price usage
                    price_return                           # price return
                ])

                period_counter+=1

    return result

def _buildLookupTable(rows,keyIndices,valueIndex):
    # rows -> {key: value}, key the tuple of fields at keyIndices. Built ONCE so a
    # per-interval lookup is O(1) instead of the O(n) linear scan this replaces,
    # repeated once per priceList interval (CODE-REVIEW.md D3 - this is also what
    # findForecast()/findAvgUsage()/findActual() each did separately, the same
    # "walk the list until the key matches" loop three times over).
    return {tuple(row[i] for i in keyIndices):row[valueIndex] for row in rows}

_locationCache=None

def solarElevation(intervalDate,intervalHr):
    # solar elevation in degrees at the midpoint of the given local hour (NOAA
    # approximation). The formula itself lives in solar.py (CODE-REVIEW.md D5) - this
    # function's own job is resolving what THIS interval means: parsing the date/hour
    # strings, adding the 30-minute hour-midpoint, and getting the site's lat/lon
    # (cached after one HTTP call per run, not one per interval).
    try:
        localDT=datetime.strptime(intervalDate+" "+intervalHr,"%Y-%m-%d %H").replace(tzinfo=ZoneInfo("Europe/Amsterdam"))
    except ValueError:
        return 90.0 # unparseable: apply no correction
    utcDT=(localDT+timedelta(minutes=30)).astimezone(ZoneInfo("UTC"))
    global _locationCache
    if _locationCache is None:
        _locationCache=getLocation() # one HTTP call per run, not one per interval
    responseResult,latitude,longitude=_locationCache
    if not responseResult:
        return 90.0
    lat,lon=float(latitude),float(longitude)
    return solar.elevation(lat,lon,utcDT)

def pvElevationCalibration(intervalDate,intervalHr):
    # fraction of the forecast this array actually delivers at this sun elevation, see
    # the pvElevationLossCurve comment block at the top of this file
    elevationDeg=solarElevation(intervalDate,intervalHr)
    return solar.interpolate(pvElevationLossCurve,elevationDeg)

def mergeForecastWithPricelist(groupSpec,forecastList,applyCalibration=False):
    # merge forecast onto pricelist as separate fields
    # applyCalibration must stay False for measured/historical values: the calibration
    # corrects forecast.solar's bias, and actuals carry no such bias
    global priceList,pvForecastRawWh
    # forecastList rows are [seqNr, forecastDate, forecastHr, forecastWh] - built once,
    # not re-scanned per interval (CODE-REVIEW.md D3).
    forecastTable=_buildLookupTable(forecastList,(1,2),3)
    for intervalNr,interval in enumerate(priceList):
        intervalDate=interval[IDX_TIME_LOCAL][0:10] # date of local time in pricelist
        intervalHr=interval[IDX_TIME_LOCAL][11:13]  # hour of local time in pricelist
        pvForecast=round(forecastTable.get((intervalDate,intervalHr),0)/intervalsPerHour()) # some rounding is o.k.
        # Keep the uncalibrated number, summed across panel groups the same way the calibrated
        # one is. Without it only the product of three multipliers is ever recorded, and
        # forecast.solar's own accuracy cannot be separated from the corrections applied to it
        # - which is exactly what pvOverallCalibration has to be fitted against. The raw
        # responses live in pv_cache/ for 48 hours and are then pruned, so every run that does
        # not record this loses the comparison permanently.
        #
        # Keyed by the interval's UTC start, not by its position: dropHistoryFromPricelist()
        # pops from the front and dropUnpublishedFromPricelist() filters, both after this runs,
        # so an index would quietly come to mean a different interval.
        pvForecastRawWh[interval[IDX_TIME_UTC]]=pvForecastRawWh.get(interval[IDX_TIME_UTC],0)+pvForecast
        if applyCalibration and pvCalibrateForecast:
            pvForecast=round(pvForecast*pvElevationCalibration(intervalDate,intervalHr)
                             *pvOverallCalibration*pvPlanningFactor)
        if groupSpec[0]=="direct":
            priceList[intervalNr][IDX_PV_DIRECT]+=pvForecast
        else:
            priceList[intervalNr][IDX_PV_INDIRECT]+=pvForecast
    if outputMode:
        for record in priceList:
            print ("merged forecast ",record)

def _mergeLoadIntoPriceList(rows,keyedByDate,label):
    # Merges a load/usage source onto priceList's IDX_LOAD field. Two shapes of source
    # reach here and both used to have their own merge function - mergeUsageWithPriceList
    # (the hourly-average FORECAST profile, ["HH", Wh] rows, repeats every day) and
    # mergeActualWithPricelist (ACTUAL usage from history, [seq, "YYYY-MM-DD", "HH", Wh]
    # rows, one instant per row). Structurally the same merge over two differently-keyed
    # sources - CODE-REVIEW.md D3.
    global priceList
    table=_buildLookupTable(rows,(1,2),3) if keyedByDate else _buildLookupTable(rows,(0,),1)
    perHour=intervalsPerHour()
    for intervalNr,interval in enumerate(priceList):
        intervalDate=interval[IDX_TIME_LOCAL][0:10] # date of local time in pricelist
        intervalHr=interval[IDX_TIME_LOCAL][11:13]  # hour of local time in pricelist
        key=(intervalDate,intervalHr) if keyedByDate else (intervalHr,)
        value=int(table.get(key,0)/perHour) # some rounding is o.k.
        priceList[intervalNr][IDX_LOAD]+=value
    if outputMode:
        for record in priceList:
            print ("merged "+label,record)

def mergeUsageWithPriceList(usageList):
    # merge (forecast) usage estimate onto pricelist as separate fields
    _mergeLoadIntoPriceList(usageList,keyedByDate=False,label="usage")

def mergeActualWithPricelist(actualList):
    # merge actual usage onto pricelist as separate fields
    _mergeLoadIntoPriceList(actualList,keyedByDate=True,label="actual")

def dropHistoryFromPricelist(runHour):
    # discard intervals of pricelist before starthour of the planning
    global priceList
    if hourAvgPlanning or runDate<datetime.strptime("20251001","%Y%m%d"):
        maxDrop=runHour
    else:
        maxDrop=4*runHour
    # pop(0) in a loop would raise IndexError the moment the price fetch returns fewer
    # intervals than maxDrop (a partial EnergyZero response, or a late-day run that
    # only got the back half of the day) - a traceback that names neither the cause
    # nor the run hour. Clamp instead, and say so: dropping fewer than requested means
    # the plan is about to start from a shorter horizon than intended, which is worth
    # knowing even though it is not fatal on its own.
    actualDrop=min(maxDrop,len(priceList))
    if actualDrop<maxDrop:
        print("WARNING: expected to drop %d interval(s) of history before hour %d, "
              "but only %d were available - the price fetch returned fewer intervals "
              "than expected."%(maxDrop,runHour,actualDrop))
    del priceList[:actualDrop]

def dropUnpublishedFromPricelist(runDate):
    # Hide prices that had not been published yet at the simulated moment. Day-ahead prices
    # for the next day appear around 13:00 local, so a replayed 06:00 run must see only the
    # run date. Inactive unless BT_ASOF_HOUR is set, so live runs and backtests are unchanged.
    global priceList
    if simulateAsOfHour<0 or simulateAsOfHour>=pricePublishHour:
        return
    runDay=datetime.strftime(runDate,'%Y-%m-%d')
    before=len(priceList)
    priceList=[interval for interval in priceList if interval[IDX_TIME_LOCAL][0:10]<=runDay]
    if outputMode or debug:
        print("as-of %02d:00: next day prices not published until %02d:00, %d of %d intervals hidden"
              %(simulateAsOfHour,pricePublishHour,before-len(priceList),before))

def dropExcludedFromPricelist():
    # discard intervals falling on dates the CSV cleaner dropped. Skipping an excluded date
    # as a runDate is not enough: the planning horizon spans ~48h, so the PREVIOUS day's run
    # still commits hours on the excluded day, where absent CSV rows read as zero load and
    # zero PV - i.e. as a free day rather than as missing data.
    global priceList
    excluded=backtestExcludedDates()
    if not excluded:
        return
    priceList=[interval for interval in priceList if interval[IDX_TIME_LOCAL][0:10] not in excluded]

def getSOC(findHour,schedule):
    # find the SOC for the given hour, searching backwards from the end of the window.
    #
    # Rewritten because the previous version, on no match, let checkRecord reach 0 and
    # then indexed priceList[checkRecord-1] == priceList[-1] - Python wraps a negative
    # index to the END of the list, so "not found" silently read back the LAST interval
    # instead of failing. Used at the multi-day backtest boundary to carry SoC into the
    # next day (main(), around the "ready for next runDate" line): a day whose price
    # list does not reach findHour would have started the next day from the wrong
    # charge, with every later day inheriting the error and nothing reporting it.
    for checkRecord in range(len(priceList)-1,-1,-1):
        if int(priceList[checkRecord][IDX_TIME_LOCAL][11:13])==findHour:
            return schedule[checkRecord]["soc"]
    raise ValueError("getSOC: no interval for hour %d in priceList (%d interval(s))"
                      %(findHour,len(priceList)))

def buildInitialPlanningList():
    # build complete list with prices, PV, usage and empty fields
        global priceList
        # start with pricelist as basis build the full list of planning intervals
        if outputMode or debug: print("Building initial list for : ",runDate)

        priceList=parsePricesIntoList(runDate,hourAvgPlanning)
        if outputMode:
            for record in priceList:
                print ("initial ",record)

        # check whether entsoe provided all expected prices, if not, get them from energyzero
        if runDate.date()==today:
            currentTime=localNow()
            currentHour=currentTime.hour
            # Was a hardcoded 15 here, disagreeing with pricePublishHour=13 - the
            # constant every comment in this project (plan-now.sh, TODO.md, the
            # cadence table) already treats as when tomorrow's day-ahead publishes.
            # Between 13:00 and 15:00 this expected only 24 intervals when 48 were
            # really available, which happens to be harmless today only because the
            # check merely decides whether to bother falling back to EnergyZero - but
            # the 14:05 run's entire purpose is to be the first to see tomorrow, and an
            # under-expectation here is exactly the condition that check exists to catch.
            if currentHour>=pricePublishHour:
                expectedIntervals=48
            else:
                expectedIntervals=24
        else:
            expectedIntervals=48
        if not hourAvgPlanning and runDate>=datetime.strptime("20251001","%Y%m%d"): expectedIntervals=expectedIntervals*4
        # note past intervals/hours will be dropped from list later
        if len(priceList)<expectedIntervals:
            priceList=getPricesFromEnergyZero(runDate,hourAvgPlanning)
            if outputMode:
                for record in priceList:
                    print ("energyzero ",record)

        # add the PV forecasts
        # with a separate field for total direct and total indirect connected PV panels

        if len(priceList)>0 and runDate.date()==today:
            if includePV:
                # A failed forecast fetch used to leave PV at zero, which the optimiser reads
                # as a heavily overcast day rather than as missing data - and then confidently
                # buys grid power to cover load the roof would have supplied. forecast.solar
                # allows only ~12 requests per hour per IP, so this is a routine failure, not
                # a rare one. Refuse to plan instead of planning on a silent zero.
                missingGroups=0
                for groupSpec in pvGroups:
                    forecastList=parsePVforecastIntoList(groupSpec)
                    if len(forecastList)>0:
                        mergeForecastWithPricelist(groupSpec,forecastList,applyCalibration=True)
                    else:
                        missingGroups+=1
                    if outputMode:
                        for record in priceList:
                            print ("merged pv ",record)
                if missingGroups:
                    print("ERROR: no PV forecast for %d of %d panel group(s)."%(missingGroups,len(pvGroups)))
                    print("       Planning on zero PV would look like a dull day and would buy")
                    print("       grid power the roof was going to supply. Refusing to plan.")
                    print("       Set BT_ALLOW_NO_PV=Y to override, or drop -p to plan without PV.")
                    if os.environ.get("BT_ALLOW_NO_PV","N").upper()!="Y":
                        raise SystemExit(3)

        # add the hourly usage forecast
            if includeUsage:
                dataFound,usageList=calcHourlyAvgUsage(homeUsageIDX,0.1)
                if dataFound:
                    mergeUsageWithPriceList(usageList)
                if outputMode:
                    for record in priceList:
                        print ("merged usage ",record)

            if outputMode:
                for record in priceList:
                    print ("merged ",record)

            dropHistoryFromPricelist(runHour)

        else:
            # get PV and actual usage for dates in the past
            if len(priceList)>0:
                if includePV:
                    pvList=getHrValueFromBIGDB(runDate,3) # pv house
                    if len(pvList)>0:
                        groupSpec=["indirect",0,0,0] # dummy
                        mergeForecastWithPricelist(groupSpec,pvList)
                    if outputMode:
                        for record in priceList:
                            print ("merged pv ",record)
                    pvList=getHrValueFromBIGDB(runDate,210) # pv blokhut
                    if len(pvList)>0:
                        groupSpec=["direct",0,0,0] # dummy
                        mergeForecastWithPricelist(groupSpec,pvList)
                    if outputMode:
                        for record in priceList:
                            print ("merged pv ",record)
                if includeUsage:
                    usageList=getHrValueFromBIGDB(runDate,22) # pv blokhut
                    if len(usageList)>0:
                        mergeActualWithPricelist(usageList)

                dropHistoryFromPricelist(runHour)
                dropUnpublishedFromPricelist(runDate)
                dropExcludedFromPricelist()

        if outputMode:
            for record in priceList:
                print ("without history ",record)
##### end of all function to collect input data   #####

def hourlyShapeFromPriceList(priceList=None, hourAvgPlanning=None):
    # average load, PV and buy price per hour-of-day across the planning window.
    # Derived from priceList itself so it works identically for a live plan (forecasts) and
    # a backtest (measured actuals), with no extra data source. counts says how many
    # intervals fed each hour, so callers can tell a measured hour from an uncovered one.
    #
    # Parameters default to the module globals (CODE-REVIEW.md A2 step 2), same pattern
    # as LPoptimization()'s _fallback: the live path, which calls this with no arguments,
    # is unaffected.
    priceList = _fallback("priceList", priceList)
    hourAvgPlanning = _fallback("hourAvgPlanning", hourAvgPlanning)
    loadSum=[0.0]*24
    pvSum=[0.0]*24
    priceSum=[0.0]*24
    counts=[0]*24
    for interval in priceList:
        hr=int(interval[IDX_TIME_LOCAL][11:13])
        loadSum[hr]+=interval[IDX_LOAD]
        pvSum[hr]+=interval[IDX_PV_DIRECT]+interval[IDX_PV_INDIRECT]
        priceSum[hr]+=interval[IDX_PRICE_BUY]
        counts[hr]+=1
    # load and PV are per interval; at 15-min planning four intervals make an hour, and the
    # reserve is expressed in whole hours, so scale back up. Price is per kWh either way.
    perHour=intervalsPerHour(hourAvgPlanning)
    loadAvg=[(loadSum[h]/counts[h])*perHour if counts[h] else 0.0 for h in range(24)]
    pvAvg=[(pvSum[h]/counts[h])*perHour if counts[h] else 0.0 for h in range(24)]
    priceAvg=[priceSum[h]/counts[h] if counts[h] else None for h in range(24)]
    return loadAvg,pvAvg,priceAvg,counts

def hoursUntilRefill(startHour,month,loadAvg,pvAvg,priceAvg,counts,
                      cheapQuantile=None,typicalCheapHourByMonth=None,reserveMaxHours=None):
    """Hours from startHour until the battery can next be refilled. Returns (hours,reason).

    Two things end the reserve period, whichever comes first:
      - the sun takes over, i.e. forecast PV exceeds forecast load
      - grid power gets cheap again

    "Cheap" is relative to the window's own price profile rather than to a fixed clock
    hour, because the cheapest hour of the day is midday for two thirds of the year here
    and pre-dawn only in Oct-Feb. Where the window covers an hour its measured price
    decides; where it does not, the month's typical cheap hour stands in.

    cheapQuantile/typicalCheapHourByMonth/reserveMaxHours default to the module globals
    (A2 step 2); loadAvg/pvAvg/priceAvg/counts stay required, as before - they are
    hourlyShapeFromPriceList()'s output, not configuration, so there is no sensible
    global to fall back to.
    """
    cheapQuantile=_fallback("cheapQuantile",cheapQuantile)
    typicalCheapHourByMonth=_fallback("typicalCheapHourByMonth",typicalCheapHourByMonth)
    reserveMaxHours=_fallback("reserveMaxHours",reserveMaxHours)
    known=[p for p in priceAvg if p is not None]
    threshold=None
    if known:
        ranked=sorted(known)
        idx=max(0,min(len(ranked)-1,int(len(ranked)*cheapQuantile)-1))
        threshold=ranked[idx]
    fallbackHour=typicalCheapHourByMonth[month-1]
    for step in range(reserveMaxHours):
        hr=(startHour+step)%24
        if pvAvg[hr]>loadAvg[hr] and pvAvg[hr]>0:
            return step,"sun takes over"
        if counts[hr]:
            if threshold is not None and priceAvg[hr]<=threshold:
                return step,"cheap hour (%.4f <= %.4f)"%(priceAvg[hr],threshold)
        elif hr==fallbackHour:
            return step,"typical cheap hour for month %02d"%month
    return reserveMaxHours,"reserveMaxHours cap"

def calcTerminalReserveWh(priceList=None,useTerminalReserve=None,reserveFloorPct=None,
                           ratedBatteryCapacity=None,reserveMarginPct=None,
                           hourAvgPlanning=None,cheapQuantile=None,
                           typicalCheapHourByMonth=None,reserveMaxHours=None):
    # how much charge must remain at the end of the planning window, in Wh
    #
    # Every parameter defaults to the module global (A2 step 2), same _fallback pattern
    # as LPoptimization() and hourlyShapeFromPriceList(). LPoptimization() passes its own
    # already-resolved priceList/ratedBatteryCapacity/hourAvgPlanning through when it
    # calls this internally, so a test that hands LPoptimization() a custom priceList and
    # leaves terminalReserveWh unset gets a reserve computed from THAT priceList, not a
    # stale or undefined module global.
    priceList=_fallback("priceList",priceList)
    useTerminalReserve=_fallback("useTerminalReserve",useTerminalReserve)
    reserveFloorPct=_fallback("reserveFloorPct",reserveFloorPct)
    ratedBatteryCapacity=_fallback("ratedBatteryCapacity",ratedBatteryCapacity)
    reserveMarginPct=_fallback("reserveMarginPct",reserveMarginPct)
    if not useTerminalReserve or len(priceList)==0:
        return 0
    floorWh=int(reserveFloorPct/100*ratedBatteryCapacity)
    loadAvg,pvAvg,priceAvg,counts=hourlyShapeFromPriceList(
        priceList=priceList,hourAvgPlanning=hourAvgPlanning)

    endHour=int(priceList[-1][IDX_TIME_LOCAL][11:13])
    # the window's last interval is the START of that hour, so the reserve begins after it
    startHour=(endHour+1)%24
    month=int(priceList[-1][IDX_TIME_LOCAL][5:7])

    hoursNeeded,reason=hoursUntilRefill(
        startHour,month,loadAvg,pvAvg,priceAvg,counts,
        cheapQuantile=cheapQuantile,typicalCheapHourByMonth=typicalCheapHourByMonth,
        reserveMaxHours=reserveMaxHours)
    if hoursNeeded==0:
        # the window already ends at the refill opportunity: only the floor applies
        if outputMode or debug:
            print("terminal reserve: window ends %02d:00, refill immediately (%s) -> floor only %d Wh"
                  %(endHour,reason,floorWh))
        return floorWh

    # the reserve covers what the grid must supply, so PV expected during the reserve
    # period counts against it - a sunny morning needs less carried through the night
    needWh=sum(max(0.0,loadAvg[(startHour+s)%24]-pvAvg[(startHour+s)%24])
               for s in range(hoursNeeded))
    needWh=needWh*(1+reserveMarginPct/100)
    reserveWh=max(int(needWh),floorWh)
    # never demand more than the battery can hold
    reserveWh=min(reserveWh,int(ratedBatteryCapacity))
    if outputMode or debug:
        print("terminal reserve: window ends %02d:00, refill in %d h (%s), "
              "net load need %d Wh, floor %d Wh -> reserve %d Wh (%.0f%% SOC)"
              %(endHour,hoursNeeded,reason,int(needWh),floorWh,reserveWh,
                reserveWh/ratedBatteryCapacity*100))
    return reserveWh

#### the actual optimsation function  #####

def _fallback(name, value):
    # value if the caller gave one, else the module-level global of the same name,
    # read at CALL time (globals() bypasses the parameter's shadowing, so this sees
    # whatever buildInitialPlanningList()/getUserInput() most recently set). The live
    # path never passes these, so it is unaffected; a test can pass a hand-built
    # priceList and every other input without touching a single module global.
    return value if value is not None else globals()[name]

def intervalsPerHour(hourAvgPlanning=None):
    # How many planning intervals make one hour: 1 in hourly mode, 4 in quarter-hour
    # mode (the default since NL day-ahead moved to a 15-minute MTU on 2025-10-01).
    #
    # CODE-REVIEW.md D2: this exact "4" used to be spelled out independently at eight
    # call sites (mergeForecastWithPricelist, mergeUsageWithPriceList,
    # mergeActualWithPricelist, hourlyShapeFromPriceList, the grid connection limit and
    # the charge/discharge bounds in LPoptimization), each guarded by its own
    # `if hourAvgPlanning:`. It is load-bearing arithmetic on money - a ninth site added
    # without the same guard would be a plan wrong by 4x with nothing to catch it.
    #
    # A quantity in Wh PER INTERVAL is an hourly Wh quantity divided by this; a
    # per-interval quantity scaled UP to an hourly one is multiplied by it.
    hourAvgPlanning = _fallback("hourAvgPlanning", hourAvgPlanning)
    return 1 if hourAvgPlanning else 4

def LPoptimization(priceList=None, initialCharge=None, ratedBatteryCapacity=None,
                    maxChargeSpeed=None, maxDischargeSpeed=None, minBatterySOCPct=None,
                    onewayEff=None, cycleCosts=None, hourAvgPlanning=None,
                    gridConnectionLimit=None, gridLimitAppliesToExport=None,
                    zeroGridCharge=None, terminalReserveWh=None):
    # lineair programming optimisationusing pulp library
    #
    # Every parameter defaults to None and falls back to the live module global (see
    # _fallback above) - this is CODE-REVIEW.md's A2 step 1. terminalReserveWh is the
    # one exception: passing it explicitly skips the calcTerminalReserveWh() call
    # entirely, which still reads its OWN globals (that is A2 step 2, not yet done) -
    # so a test that wants to check the reserve constraint in isolation, without also
    # having to fake calcTerminalReserveWh()'s inputs, can just pass the number it
    # wants enforced.
    priceList = _fallback("priceList", priceList)
    initialCharge = _fallback("initialCharge", initialCharge)
    ratedBatteryCapacity = _fallback("ratedBatteryCapacity", ratedBatteryCapacity)
    maxChargeSpeed = _fallback("maxChargeSpeed", maxChargeSpeed)
    maxDischargeSpeed = _fallback("maxDischargeSpeed", maxDischargeSpeed)
    minBatterySOCPct = _fallback("minBatterySOCPct", minBatterySOCPct)
    onewayEff = _fallback("onewayEff", onewayEff)
    cycleCosts = _fallback("cycleCosts", cycleCosts)
    hourAvgPlanning = _fallback("hourAvgPlanning", hourAvgPlanning)
    gridConnectionLimit = _fallback("gridConnectionLimit", gridConnectionLimit)
    gridLimitAppliesToExport = _fallback("gridLimitAppliesToExport", gridLimitAppliesToExport)
    zeroGridCharge = _fallback("zeroGridCharge", zeroGridCharge)

    nrIntervals = len(priceList)
    if nrIntervals == 0:
        # CBC solves a problem with zero variables as "Optimal" and returns an empty
        # schedule, which looks exactly like a healthy but very short plan everywhere
        # downstream: outputOptimisationResult() writes a header-only file, plan-now.sh
        # renames it into plans/, and only advise.py's --min-hours guard stood between
        # that and a scheduled run reporting success. A total input failure (both
        # ENTSOE and EnergyZero returning nothing) should look like a failure here,
        # at the source, not rely on a downstream check to catch it.
        print("ERROR: no price intervals to plan - refusing to solve an empty problem.")
        print("       Both price sources (ENTSOE, EnergyZero) returned nothing for this window.")
        raise SystemExit(5)

    # BATTERY PARAMETERS, RTE split into equal parts for charge and discharge
    Effcharge = onewayEff
    Effdischarge = onewayEff

    # Local, readable aliases for the module-level IDX_* constants (CODE-REVIEW.md D1) -
    # kept because "forecastDirectIndex" reads better in the LP constraints below than
    # "IDX_PV_DIRECT" would, not because the numbers themselves need re-declaring here.
    forecastDirectIndex=IDX_PV_DIRECT
    forecastIndirectIndex=IDX_PV_INDIRECT
    forecastUsageIndex=IDX_LOAD
    buyPriceIndex=IDX_PRICE_BUY
    sellPriceIndex=IDX_PRICE_SELL

    # LP PROBLEM, we are aiming for maximum financial return
    prob = pulp.LpProblem("Battery_Optimization", pulp.LpMaximize)


    # VARIABLES
    #
    # upBound is the per-INTERVAL cap, not the raw hourly maxChargeSpeed/maxDischargeSpeed -
    # CODE-REVIEW.md D6. This used to declare the bound as the full hourly value here and
    # then separately constrain chargeWh[t]/dischargeWh[t] to maxChargeSpeed/intervalsPerHour()
    # inside the per-interval loop below. Both were correct together (the tighter constraint
    # always won), but the variable's OWN bound said something false in quarter-hour mode -
    # "this can be 4850 Wh in a 15-minute interval" - and a future edit that dropped the loop
    # constraint as apparently redundant with this one would have quadrupled the charge rate
    # rather than caught nothing. One correct number, declared once.
    chargeWh = pulp.LpVariable.dicts("charge", range(nrIntervals), lowBound=0, upBound=maxChargeSpeed/intervalsPerHour(hourAvgPlanning)) # this is indirect charge from PV not connected directly
    dischargeWh = pulp.LpVariable.dicts("discharge", range(nrIntervals), lowBound=0, upBound=maxDischargeSpeed/intervalsPerHour(hourAvgPlanning))
    socFloorWh=int(float(minBatterySOCPct/100*ratedBatteryCapacity))
    sockWh = pulp.LpVariable.dicts("soc", range(nrIntervals), lowBound=socFloorWh, upBound=ratedBatteryCapacity)
    # The minimum-SOC rule is a rule for the plan, not a description of the battery. If the
    # battery is ACTUALLY below the floor when planning starts, applying the floor to the
    # first interval too makes the whole problem infeasible and the planner emits nothing -
    # exactly when a plan is most needed. Let interval 0 accept reality; every later
    # interval keeps the floor, so the plan climbs back at the first opportunity.
    if nrIntervals>0 and initialCharge is not None and int(initialCharge)<socFloorWh:
        sockWh[0].lowBound=int(initialCharge)
        if outputMode or debug:
            print("initial charge %d Wh is below the %d%% floor (%d Wh); relaxing the floor for "
                  "the first interval only"%(int(initialCharge),minBatterySOCPct,socFloorWh))
    # Grid connection limit. maxChargeSpeed bounds the battery; this bounds the meter, and
    # the two are different numbers because the house load rides on the same fuse. Expressed
    # per interval like the charge caps below: a Watt limit is Wh/interval only when the
    # interval is an hour. 0 (or None) leaves import/export unbounded, as before.
    if gridConnectionLimit:
        gridLimitPerInterval=gridConnectionLimit/intervalsPerHour(hourAvgPlanning)
    else:
        gridLimitPerInterval=None
    importWh = pulp.LpVariable.dicts("import", range(nrIntervals), lowBound=0, upBound=gridLimitPerInterval)
    exportWh = pulp.LpVariable.dicts("export", range(nrIntervals), lowBound=0,
                                     upBound=gridLimitPerInterval if gridLimitAppliesToExport else None)
    # A plain dict, not an LpVariable.dicts() - CODE-REVIEW.md D6. The previous LpVariable
    # version was never added to `prob` (no constraint ever tied it to anything) and was
    # immediately overwritten by the plain expression below before ever being solved for,
    # so it existed only as N unused solver variables and a misleading declaration.
    costsEuro = {}

    # OBJECTIVE, maximise income minus costs
    # note variables contain Wh values and all prices are kWh prices, so should be divided by factor 1000, but optimisation
    # in extreme cases does not optimise properly then (due to small floating point numbers), so the factor 1000 is removed (does not matter for optimisation)
    # costsEuro variable is calculated with correct factor 1000, see below
    prob += pulp.lpSum(
        priceList[t][sellPriceIndex] * exportWh[t] - priceList[t][buyPriceIndex] * importWh[t] - dischargeWh[t]* cycleCosts # note factor 1000 on all costs removed
        for t in range(nrIntervals)
    )


    # CONSTRAINTS
    for t in range(nrIntervals):
        # Energy balance , note could remove priceList[t][forecastDirectindex] on both sides of == sign
        prob += (
            priceList[t][forecastDirectIndex] + priceList[t][forecastIndirectIndex] + importWh[t] + dischargeWh[t]
            ==
            priceList[t][forecastUsageIndex] + exportWh[t] + chargeWh[t] + priceList[t][forecastDirectIndex]
        )

        # SOC evolution, make the connection between intervals
        if t == 0:
            prob += sockWh[t] == initialCharge + priceList[t][forecastDirectIndex]+Effcharge * chargeWh[t] - dischargeWh[t] / Effdischarge
        else:
            prob += sockWh[t] == sockWh[t-1] + priceList[t][forecastDirectIndex]+Effcharge * chargeWh[t] - dischargeWh[t] / Effdischarge


        # calculate actuals costs  with correct factor 1000
        costsEuro[t]=priceList[t][sellPriceIndex]/1000 * exportWh[t] - priceList[t][buyPriceIndex]/1000 * importWh[t]

        # constraint if import from grid is not allowed (but note optimisation might not be possible then)
        if zeroGridCharge:
            prob += importWh[t]==0

    # TERMINAL RESERVE, keep enough charge at the end of the window to cover the gap until
    # the next refill opportunity. Without it the objective values leftover energy at zero
    # and sells the battery down to the floor in the final hours.
    if terminalReserveWh is None:
        # Pass the values THIS call already resolved (see the _fallback block above),
        # not bare globals - so a test that hands LPoptimization() a custom priceList
        # and leaves terminalReserveWh unset gets a reserve computed from that
        # priceList, and the live path (nothing passed in, everything read from the
        # module globals) is unchanged.
        terminalReserveWh=calcTerminalReserveWh(
            priceList=priceList,ratedBatteryCapacity=ratedBatteryCapacity,
            hourAvgPlanning=hourAvgPlanning)
    if terminalReserveWh>0 and nrIntervals>0:
        prob += sockWh[nrIntervals-1] >= terminalReserveWh

    # SOLVE, run the solver
    prob.solve(pulp.PULP_CBC_CMD(msg=False))

    optimisationStatus=pulp.LpStatus[prob.status]

    # OUTPUT, put the variables for each interval onto a list called "schedule"
    schedule = []
    for t in range(nrIntervals):
        schedule.append({
            "interval": t,
            "charge": int(chargeWh[t].value()),
            "discharge": int(dischargeWh[t].value()),
            "soc": int(sockWh[t].value()),
            "import": int(importWh[t].value()),
            "export": int(exportWh[t].value()),
            "costs" : int(costsEuro[t].value()*10000)/10000,
            # Constant across the horizon - it is one boundary condition, not a per-interval
            # decision. Repeated on every point so a dashboard can draw it as a line against
            # soc without joining a second query.
            "reserve": terminalReserveWh
            })

    return optimisationStatus,schedule

#### end of optimisation, beginning of output functions   #####

def _rowsToOutput(runDate,startDateObject,endDateObject,priceList,schedule):
    """Which (nr, record) pairs from `schedule` this run's file output should include.

    A BACKTEST-CHAINING convention, not a live-plan concern (CODE-REVIEW.md D8): a
    multi-day backtest writes one growing file across several runDate iterations, and
    every iteration after the first owns only the slice from 15:00 (when the next
    day's prices become known) onward, so it does not re-print intervals the previous
    iteration already wrote. plan-now.sh dodges all three branches by setting
    BT_END=tomorrow, which always takes the first ("everything") branch below - see
    its own comment for why a live plan would otherwise be cut short at 15:00.
    """
    if runDate+timedelta(days=1)==endDateObject:
        # last day of the run (or the live path, in practice): everything
        return list(enumerate(schedule))
    runDateString=datetime.strftime(runDate,'%Y-%m-%d')
    if runDate==startDateObject:
        # first day: from the start, up to (excl) 15:00 the next day
        return [(nr,record) for nr,record in enumerate(schedule)
                if priceList[nr][IDX_TIME_LOCAL][0:10]==runDateString
                or str(priceList[nr][IDX_TIME_LOCAL][11:13])<"15"]
    nextDateString=datetime.strftime(runDate+timedelta(days=1),'%Y-%m-%d')
    # a middle day: from 15:00 runDate (incl) to 15:00 next day (excl)
    return [(nr,record) for nr,record in enumerate(schedule)
            if (priceList[nr][IDX_TIME_LOCAL][0:10]==runDateString and str(priceList[nr][IDX_TIME_LOCAL][11:13])>="15")
            or (priceList[nr][IDX_TIME_LOCAL][0:10]==nextDateString and str(priceList[nr][IDX_TIME_LOCAL][11:13])<"15")]

def outputOptimisationResult(optimisationStatus,schedule,outputFileName,writeMode):
    # output to a file. `with`, not a bare open()/close() (CODE-REVIEW.md D8) - an
    # exception mid-write used to leave the handle open and the file truncated.
    rows=_rowsToOutput(runDate,startDateObject,endDateObject,priceList,schedule)
    isLastDay=(runDate+timedelta(days=1)==endDateObject)
    with open(outputFileName,writeMode) as fileHandle:
        if optimisationStatus!="Optimal":
            print ("ATTENTION: no optimal solution achieved, status is ",optimisationStatus," on date ",runDate,file=fileHandle)
        if runDate==startDateObject:
            print("date        time   pvD   pvI   use  nett chrgD  chrg dschg   soc   imp   exp  pr-buy pr-sell    cost",file=fileHandle)
        totalCosts=0
        for nr,record in rows:
            totalCosts+=record["costs"]
            printIntervalToFile(nr,record,fileHandle)
        if isLastDay:
            print("Total costs ",totalCosts)

def printIntervalToFile(nr,record,fileHandle):
    # output one single line to a file
    print( priceList[nr][IDX_TIME_LOCAL]+" "+"{:>5d}".format(priceList[nr][IDX_PV_DIRECT])+" "+"{:>5d}".format(priceList[nr][IDX_PV_INDIRECT])+" "+"{:>5d}".format(priceList[nr][IDX_LOAD])+" "+"{:>5d}".format(priceList[nr][IDX_LOAD]-priceList[nr][IDX_PV_INDIRECT]-priceList[nr][IDX_PV_DIRECT]), end=" ",file=fileHandle)
    print("{:>5d}".format(priceList[nr][IDX_PV_DIRECT])+" "+"{:>5d}".format(record["charge"])+" "+"{:>5d}".format(record["discharge"])+" "+"{:>5d}".format(record["soc"])+" "+"{:>5d}".format(record["import"])+" "+"{:>5d}".format(record["export"]),end=" ",file=fileHandle)
    print("{:>+1.6f}".format(priceList[nr][IDX_PRICE_BUY])+" "+"{:>+1.6f}".format(priceList[nr][IDX_PRICE_SELL])+" "+"{:>+2.6f}".format(record["costs"]),file=fileHandle)

def appSettingLines(planRows,planRun):
    # The plan follows no price threshold - it solves the whole horizon - but the alphaess
    # app can only be given one sell-above/buy-below pair at a time. app_bands works
    # backwards from the plan to the pair that reproduces it, per trading session, so the
    # app can be retuned by hand a couple of times a day instead of guessed at.
    #
    # Written here rather than derived in the dashboard's Flux because the interesting part
    # is not the query but the arithmetic: whether a threshold exists that trades in exactly
    # the planned intervals and no others. That needs tests over seeded scenarios, and a
    # query buried in generated dashboard JSON cannot have any.
    if not planRows:
        return []
    out=[]
    for s in app_bands.appSettings(planRows):
        out.append(influx_source.linePoint("app_setting",
            {"plan_run":planRun,"action":s["action"]},
            {"set_to_eur_kwh":s["setTo"],
             # Stored as seconds rather than a timestamp because an Influx field cannot
             # hold a time; the dashboard turns it back into one.
             "until_s":int(s["until"].timestamp()),
             "target_soc_wh":s["targetSocWh"],
             # False when no single threshold reproduces the plan over the window this
             # setting is live for. `extra` counts the intervals that then trade against
             # the plan's wishes - the dashboard shows both rather than hiding a number
             # that cannot do what it claims.
             "exact":1 if s["exact"] else 0,
             "extra_intervals":s["extra"],
             "intervals":s["intervals"],
             "energy_wh":s["energyWh"]},
            s["start"]))
    return out


def writePlanToInflux(schedule,planRun):
    # Record the plan beside the actuals, so "what did we intend at 14:05?" is a query rather
    # than a hunt through text files, and so plan-vs-actual becomes one dashboard instead of
    # a comparison between a file and a graph.
    #
    # pv_forecast_wh is the reason to do this early: nothing else stores the forecast
    # anywhere, so every day it is not written is a day the forecast-vs-actual calibration
    # can never be run for. That data does not become available again later.
    #
    # Deliberately non-fatal. By the time this runs the plan is already printed and on disk;
    # a missing dashboard point is a smaller loss than a planning run that dies at the last
    # step, and this is the step that depends on a service being up.
    if not writePlansToInflux or influx_source is None or not influxAvailable:
        return
    lines=[]
    planRows=[]
    for nr,record in enumerate(schedule):
        if nr>=len(priceList): break
        try:
            # priceList[nr][IDX_TIME_UTC] is the interval start in UTC, which is the unambiguous one.
            # The local string beside it repeats itself on the October DST night.
            when=datetime.strptime(priceList[nr][IDX_TIME_UTC],"%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        lines.append(influx_source.linePoint("plan",
            {"plan_run":planRun},
            {"soc_wh":record["soc"],
             "charge_wh":record["charge"],
             "discharge_wh":record["discharge"],
             "import_wh":record["import"],
             "export_wh":record["export"],
             "cost_eur":record["costs"],
             "reserve_wh":record.get("reserve",0),
             "price_buy":priceList[nr][IDX_PRICE_BUY],
             "price_sell":priceList[nr][IDX_PRICE_SELL],
             # The raw market price the two above are built from, stored as well because the
             # dashboard draws that one - it is the signal the alphaess app's High/Low bands are
             # set against, and price_buy/price_sell cannot be turned back into it (tax, VAT and
             # saldering are not invertible per interval). Until now that line came from the
             # collector's own Frank feed, refreshed every 3 hours, so tomorrow's half of the
             # horizon was blank for hours after the plan had already optimised against it -
             # and even once filled it was a second feed that need not agree slot for slot.
             # Same value planRows uses below, so the line and the bands cannot drift apart.
             "price_market":priceList[nr][IDX_PRICE_KWH],
             # The two panel groups are one roof as far as any dashboard is concerned.
             "pv_forecast_wh":priceList[nr][IDX_PV_DIRECT]+priceList[nr][IDX_PV_INDIRECT],
             # The same forecast before pvElevationCalibration(), pvOverallCalibration and
             # pvPlanningFactor are applied. Storing only the product makes forecast accuracy
             # and correction accuracy indistinguishable: an evening hour can read 74% under
             # while the raw forecast was 56% under and the elevation curve supplied the rest.
             # pvOverallCalibration is still an unfitted 1.00 and has to be fitted against the
             # raw number, so without this the fit has no input that outlives the 48-hour
             # pv_cache retention.
             "pv_forecast_raw_wh":pvForecastRawWh.get(priceList[nr][IDX_TIME_UTC],0),
             "load_forecast_wh":priceList[nr][IDX_LOAD]},
            when))
        planRows.append({"ts":when,
                         # Market price, not IDX_PRICE_BUY: the alphaess app's High/Low
                         # bands are set against the market signal, not the all-in price
                         # this optimiser minimises.
                         "price":priceList[nr][IDX_PRICE_KWH],
                         "charge":record["charge"],
                         "discharge":record["discharge"],
                         "import":record["import"],
                         "export":record["export"],
                         "soc":record["soc"]})
    lines.extend(appSettingLines(planRows,planRun))
    if not lines:
        return
    try:
        written=influx_source.writePoints(lines)
        print("InfluxDB: wrote %d plan intervals to bucket %s (plan_run=%s)"
              %(written,influx_source.config()["plan_bucket"],planRun))
    except Exception as e:
        print("WARNING: could not write the plan to InfluxDB (%s)."%e)
        print("         The plan itself is unaffected - it is printed above and in the output")
        print("         file. Only the stored copy the dashboard reads is missing.")

def outputToTextDevice(schedule,starthour,writeMode,optimisationStatus):
    # output to domoticz text device
    if writeMode=='w':
        clearTextDevice(planningDisplayIDX)
    totalCosts=0
    for nrReversed,record in enumerate(reversed(schedule)):
            nr=len(priceList)-nrReversed-1
            outputString="%10s %5d %5d %5d %5d %5d %5d %5d %5d %6d %6d  %1.4f  %1.4f  %2.4f" %(priceList[nr][IDX_TIME_LOCAL],priceList[nr][IDX_PV_DIRECT],priceList[nr][IDX_PV_INDIRECT],priceList[nr][IDX_LOAD],priceList[nr][IDX_LOAD]-priceList[nr][IDX_PV_INDIRECT]-priceList[nr][IDX_PV_DIRECT],priceList[nr][IDX_PV_DIRECT],record["charge"],record["discharge"],record["soc"],record["import"],record["export"],priceList[nr][IDX_PRICE_BUY],priceList[nr][IDX_PRICE_SELL],record["costs"])
            outputString=outputString.replace(' ','_')  # JSON processing removes all duplicate spaces, so use underscore to get table format
            setTextDevice(planningDisplayIDX,outputString)
            totalCosts+=record["costs"]
    timestamp=datetime.strftime(localNow(),'%Y%m%d %H:%M:%S')
    setTextDevice(planningDisplayIDX,"date________time___pvD___pvI___use___nett__chrgD__chrg__dscg___soc____imp____exp___pr-buy__pr-sell____cost")
    if optimisationStatus!="Optimal":
        setTextDevice(planningDisplayIDX,"ATTENTION: no optimal solution achieved, status is "+optimisationStatus)
    setTextDevice(planningDisplayIDX,"total costs "+str(totalCosts))
    setTextDevice(planningDisplayIDX,"****** planning created "+timestamp+" for period "+startdate+" "+str(starthour)+" hr to "+enddate+" 24:00 hr ******")

def outputToBattery(schedule,starthour,result):
    # send required action for next hour to the battery
    scheduleCurrentHr=schedule[0]
    priceListCurrentHr=priceList[0]
    scheduleDateTime=priceListCurrentHr[IDX_TIME_LOCAL]
    pvDirect=priceListCurrentHr[IDX_PV_DIRECT]
    pvIndirect=priceListCurrentHr[IDX_PV_INDIRECT]
    usageForecast=priceListCurrentHr[IDX_LOAD]
    chargeIndirect=scheduleCurrentHr["charge"]
    discharge=scheduleCurrentHr["discharge"]
    importWh=scheduleCurrentHr["import"]
    exportWh=scheduleCurrentHr["export"]
    soc=scheduleCurrentHr["soc"]
    # !!! to be done: this assume the planning is done at the beginning of the hour, to be adapted for partial hours when running at random time
    if importWh==0 and exportWh==0: #self-consumption
        print("next hour",priceListCurrentHr[IDX_TIME_LOCAL]," action ","self-consumption")
        setBatteryAction("AutoSelf",scheduleDateTime,0,schedule)
    else:
        if chargeIndirect==0 and discharge==0: # passive
            print("next hour",priceListCurrentHr[IDX_TIME_LOCAL]," action ","passive")
            setBatteryAction("Passive",scheduleDateTime,0,schedule)
        else:
            if chargeIndirect>0: # manual charge
                print("next hour",priceListCurrentHr[IDX_TIME_LOCAL]," action ","manual charge ",-1*chargeIndirect) # charge must be negative value
                setBatteryAction("Manual",scheduleDateTime,-1*chargeIndirect,schedule)
            else:
                if discharge>0: # manual discharge
                    #if discharge==maxDischargeSpeed or round((soc-discharge/onewayEff)/ratedBatteryCapacity,0)==minBatterySOCPct:
                    #    print("next hour",priceListCurrentHr[IDX_TIME_LOCAL]," action ","self-consumption",discharge)
                    #    setBatteryAction("AutoSelf",scheduleDateTime,0,schedule)
                    #else:
                    print("next hour",priceListCurrentHr[IDX_TIME_LOCAL]," action ","manual discharge ",discharge)
                    setBatteryAction("Manual",scheduleDateTime,discharge,schedule)
                else:
                    print("next hour",priceListCurrentHr[IDX_TIME_LOCAL]," action ","don't know")
                    # don't know, should not exist

def processCLarguments():
    # get command line arguments to determine the run modes
    global debug,outputMode,runMode,includePV,includeUsage,zeroGridCharge,includeTax,saldering,hourAvgPlanning,mqttQuery
    debug=False
    outputMode=False
    runMode="standalone"
    includePV=False
    includeUsage=False
    zeroGridCharge=False
    includeTax=False
    saldering=False
    hourAvgPlanning=False
    CLargSuccess=True
    mqttQuery=False
    try:
        for i in range(len(sys.argv)-1):
            if sys.argv[i+1] not in ["-t","-v","-q","-d","-s","-i","-p","-u","-z","-b","-n","-h","-m"]:
                raise Exception
            # use one of the 3 next arguments to set output level
            if sys.argv[i+1]=="-t": # trace
                debug=True
                outputMode=True
            if sys.argv[i+1]=="-v": # verbose
                debug=False
                outputMode=True
            if sys.argv[i+1]=="-q": # quiet
                debug=False
                outputMode=False
            # choose between domoticz integrated or standalone
            if sys.argv[i+1] in ["-d","-i"] and not useDomoticz:
                # both modes source their data from Domoticz; refuse rather than silently
                # falling back, so a stale command line cannot produce a bogus plan
                print("ERROR: "+sys.argv[i+1]+" needs Domoticz, which is disabled (useDomoticz=False).")
                print("       Use -s (standalone), or set useDomoticz=True at the top of this file.")
                raise Exception
            if sys.argv[i+1]=="-d": # domoticz
                runMode="domoticz"
            if sys.argv[i+1]=="-s": # standalone
                runMode="standalone"
            if sys.argv[i+1]=="-i": # integrated (=from command line but data from domoticz)
                runMode="integrated"

            # include PV forecast/actual
            if sys.argv[i+1]=="-p": # include PV forecast/actual
                includePV=True

            # include usage estimate/actual
            if sys.argv[i+1]=="-u": # include usage
                includeUsage=True

            # block charging from grid
            if sys.argv[i+1]=="-z": # zero grid
                zeroGridCharge=True # charging from grid is not allowed, only from PV

            # include tax elements in price
            if sys.argv[i+1]=="-b": # belasting
                includeTax=True

            # set whether saldering/netting applies
            if sys.argv[i+1]=="-n": # netting/saldering
                saldering=True
                # keep the flag meaning "force it on", overriding the date rule
                globals()["salderingMode"]="on"

            # set planning interval qtr (=15min) or hour
            if sys.argv[i+1]=="-h": # plan with hr avg even if 15 min data available
                hourAvgPlanning=True

            if sys.argv[i+1]=="-m": # mqtt marstek querying
                mqttQuery=True

    except Exception:
        print("Following command line arguments are recognised: -t,-v,-q and -d,-s and -p and -z and -b and -n and -h")
        print("-t = full tracing, debug mode")
        print("-v = verbose mode, intermediate steps in planning are shown")
        print("-q = quiet mode (default), no intermediate feedback provided.")
        print(" ")
        print("-d = domoticz integration mode")
        print("-s = standalone mode, no domoticz integration (default)")
        print(" ")
        print("-p = PV to be included (default NO)")
        print("     When running domoticz mode , PV forecast is included, otherwise PV actual surplus")
        print("     Note that actual PV surplus data is retrieved from Domoticz even if standalone")
        print("-u = include expected usage estimate.")
        print("-z = zero charging from grid")
        print("-b = include tax elements in price")
        print("-n = force saldering/netting on, overriding the date rule")
        print("     By default saldering is decided per interval: on before "+salderingEndDate+",")
        print("     off from then. Set BT_SALDERING=auto|on|off to control it directly.")
        print("-h = plan hourly intervals instead of 15 minute")
        print("-m = use Marstek mqtt query to get required start data")
        print("     ")
        CLargSuccess=False
    return CLargSuccess

def main():
    # main contrrol loop
    global startdate,enddate,starthour,initialCharge,includePV,includeUsage,zeroGridCharge,runDate,runHour,includeTax,energyTax,vatPCT,xmlAvailable,hourAvgPlanning,startDateObject,endDateObject

    if not processCLarguments():
        quit()

    if useDomoticz and (runMode=="domoticz" or runMode=="integrated"):
        if not getPlanningInput():
            print("ERROR: Something wrong with getting all planning input data.")
            quit() # no point in going further
        xmlAvailable="N"
        overwrite="Y"
    else:
        getUserInput()
        xmlAvailable=_ask("BT_XMLAVAIL","Is the xml-data already available in the file(s) Y/N ? (default N) ","N")
        overwrite=_ask("BT_OVERWRITE","Overwrite previous output file(s) Y/N ? (default Y) ","Y")
    if overwrite=="Y" or overwrite=="y":
        writeMode='w'
    else:
        writeMode='a'

    if runMode=="standalone" or runMode=="integrated":
        outputFileName="entsoe-output"+startdate+".txt"
        fileHandle = open(outputFileName, writeMode)
        #print("date        time   pv   pv  use nett chrg dscg  soc   imp   exp  pr-buy pr-sell    cost",file=fileHandle)
        fileHandle.close()
        writeMode='a'
    else:
        outputFileName=None

    # prepare objects for use of the for-loop
    startDateObject=datetime.strptime(startdate,'%Y%m%d')
    endDateObject=datetime.strptime(enddate,'%Y%m%d')
    runDate=startDateObject
    runHour=starthour
    # Identifies this run in InfluxDB. Taken once, before the loop, so every interval of one
    # plan carries the same tag - that is what "show me the current plan" filters on.
    #
    # UTC, not local. The tag is a string, and picking "the newest plan" means sorting those
    # strings - a tag carries no other order. Local time breaks that exactly once a year: on
    # the October DST night the 02:05 run is stamped +02:00 and the 02:05 run an hour later
    # +01:00, which sorts LOWER despite being later, so every consumer would quietly show the
    # stale plan until 05:05. In UTC, lexicographic order is chronological order, always.
    planRunStamp=datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")

    while runDate<endDateObject or runDate==startDateObject:

        # skip days the CSV cleaner dropped: carry SOC forward but plan nothing, so the
        # missing data does not enter the backtest result as a zero-load day
        # (the set is empty unless BACKTEST_CSV points at a cleaned CSV with a sidecar,
        #  so this is inert for live/forecast runs)
        if runDate.date()!=today and datetime.strftime(runDate,'%Y-%m-%d') in backtestExcludedDates():
            if outputMode or debug: print("Skipping excluded date : ",runDate)
            runDate=runDate+timedelta(days=1)
            if runDate<endDateObject:
                runHour=15
                writeMode='a'
            continue

        # setting the output variables and getting external data
        if outputMode or debug: print("Processing : ",runDate," from hour ",runHour)

        buildInitialPlanningList()
        result,schedule=LPoptimization()
        if runMode!="domoticz":
            print(datetime.strftime(runDate,'%Y%m%d'))
            outputOptimisationResult(result,schedule,outputFileName,writeMode)
        # Live runs only. The loop below walks a date range, so a backtest would otherwise
        # write hundreds of replayed days into the bucket under one plan_run stamp.
        if runDate.date()==today:
            writePlanToInflux(schedule,planRunStamp)
        # prepare for next day run
        runDate=runDate+timedelta(days=1)
        if runDate<endDateObject:
            runHour=15
            initialCharge=getSOC(runHour-1,schedule)
            writeMode='a'
            if outputMode or debug: print("ready for next runDate ",datetime.strftime(runDate,'%Y%m%d')," with initialCharge ",initialCharge," at 15:00")

            if debug: input("Enter to continue ... *****************************************************************************************************************************************************************************************************")


    if useDomoticz and runMode=="domoticz":
        # writes the planning to a Domoticz text device and pushes the action to the battery
        outputToTextDevice(schedule,starthour,'w',result)
        outputToBattery(schedule,starthour,result)

if __name__ == '__main__':
    main()

