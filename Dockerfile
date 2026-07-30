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
#
# gosu is how entrypoint.sh drops from root to the UID that owns the data mount. `su` brings
# a login session, a PAM stack and its own signal handling along with it; gosu execs and gets
# out of the way, which is what a one-shot batch container wants.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata gosu \
 && rm -rf /var/lib/apt/lists/* \
 && gosu nobody true

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

COPY *.py plan-now.sh solar-forecast.sh entrypoint.sh ./
RUN chmod +x plan-now.sh solar-forecast.sh entrypoint.sh \
 && mkdir -p /data

# Everything the planner writes is CWD-relative, and BT_DATA_DIR above sends plan-now.sh
# here. A bind mount over /data replaces this directory's ownership with the host's, which is
# why there is no USER line: the image cannot know that UID at build time, and guessing it
# wrong used to fail silently. entrypoint.sh reads the owner off the mount at startup, becomes
# that user, and refuses to run if the result still cannot write.
WORKDIR /data

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["/app/plan-now.sh"]
