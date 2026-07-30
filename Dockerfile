# The advisory planner, for unattended 3-hourly runs on the Synology NAS.
# Advice only - nothing is ever sent to the battery.
#
# One run per container: it plans, prints, writes to /data and exits. Scheduling belongs to
# DSM Task Scheduler, not to a long-lived process - see NAS-DEPLOYMENT-PLAN.md section 3.
FROM python:3.12-slim

# tzdata as an OS package, not merely the pip one in requirements.txt. plan-now.sh reads the
# clock with the shell's `date`, which resolves TZ against /usr/share/zoneinfo; the pip
# package is visible only to Python's zoneinfo. Install just one and the shell and the
# planner end up in different timezones - precisely the failure the BT_TZ work prevents.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

ENV TZ=Europe/Amsterdam \
    BT_TZ=Europe/Amsterdam \
    BT_DATA_DIR=/data \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Requirements first, so a code edit does not re-resolve and re-download the dependency set.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Prove the solver actually solves, at build time. PuLP ships its own CBC binary and the
# DS220+ is x86_64 (Celeron J4025), so this is expected to pass - but a solver that installs
# and then cannot execute is exactly the kind of thing to discover here rather than at 02:05
# on a schedule, where the only symptom would be a missing plan.
RUN python -c "import pulp; \
p=pulp.LpProblem('smoke',pulp.LpMaximize); \
x=pulp.LpVariable('x',0,4); y=pulp.LpVariable('y',0,4); \
p += x+y; p += x+2*y<=4; \
st=p.solve(pulp.PULP_CBC_CMD(msg=0)); \
assert pulp.LpStatus[st]=='Optimal', 'CBC status: '+pulp.LpStatus[st]; \
assert abs(pulp.value(p.objective)-4.0)<1e-6, 'CBC objective: '+str(pulp.value(p.objective)); \
print('CBC smoke test OK')"

# Confirm the timezone resolves in this image, for the same reason: a slim base without
# tzdata raises ZoneInfoNotFoundError, and the planner would fall back to the system clock
# with only a warning nobody reads.
RUN python -c "from zoneinfo import ZoneInfo; from datetime import datetime; \
print('timezone OK:', datetime.now(ZoneInfo('Europe/Amsterdam')).strftime('%Y-%m-%d %H:%M %Z'))"

COPY *.py plan-now.sh solar-forecast.sh ./
RUN chmod +x plan-now.sh solar-forecast.sh

# Non-root, with a UID that must match the owner of the bind-mounted /data.
#
# Get this wrong and the failure is SILENT: Marstek-planning.py wraps its os.makedirs calls
# in a bare `except: pass`, so an unwritable mount means every run refetches instead of
# caching, and the visible symptom is forecast.solar rate-limiting - which looks like an API
# problem, not a permissions one. Check `ls -n` on the host directory and match it:
#   sudo docker compose build --build-arg PLANNER_UID=1026 --build-arg PLANNER_GID=100
ARG PLANNER_UID=1000
ARG PLANNER_GID=1000
RUN set -eu; \
    if ! getent group "${PLANNER_GID}" >/dev/null; then groupadd -g "${PLANNER_GID}" planner; fi; \
    if ! getent passwd "${PLANNER_UID}" >/dev/null; then \
        useradd -u "${PLANNER_UID}" -g "${PLANNER_GID}" -m planner; fi; \
    mkdir -p /data; \
    chown "${PLANNER_UID}:${PLANNER_GID}" /data
USER ${PLANNER_UID}:${PLANNER_GID}

# Everything the planner writes is CWD-relative, and BT_DATA_DIR above sends plan-now.sh
# here. A bind mount over /data replaces the ownership set above with the host's, which is
# why the UID has to match rather than merely existing.
WORKDIR /data

CMD ["/app/plan-now.sh"]
