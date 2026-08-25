"""Shared outbound HTTP policy - timeouts and retries (CODE-REVIEW.md E7, E9).

(connect, read) seconds. A scheduled job that hangs forever is worse than one that
fails: nothing reports it, and the next run stacks up behind it. Applied to every
outbound call this project makes - forecast.solar, ENTSOE and EnergyZero from
planner.py, and InfluxDB's query/write endpoints from influx_source.py.

Before this, planner.py declared (10, 30) once and influx_source.py
separately hardcoded a bare 30 (connect and read together) at its two live call
sites - the same policy expressed twice, with no guarantee an edit to one would ever
reach the other.
"""
import time
import urllib.parse

import requests

HTTP_TIMEOUT = (10, 30)

# Retry policy for the live path (CODE-REVIEW.md E9). On 2026-08-18 the 13:05 run died
# outright on `Temporary failure in name resolution` reaching EnergyZero - the whole plan
# lost, thirteen seconds after it started, to a blip that had cleared long before anybody
# looked. The planner runs hourly and holds a 20-minute lock (scripts/plan.sh), so it can
# afford to wait out a blip; what it cannot afford is to treat one as a permanent failure.
#
# One sleep per retry, in seconds, so len() is also the number of retries. The ladder is
# deliberately not exponential-from-1: a DNS failure returns in milliseconds, so a first
# retry that fires immediately just asks the same broken resolver the same question. Five
# seconds is long enough for a DHCP lease renewal or a router reboot to finish; the whole
# ladder spends 50 s, which is the "about a minute" a transient home-network fault takes to
# clear. Beyond that it is not transient, and failing the run is the honest answer.
HTTP_RETRY_BACKOFF_S = (5, 15, 30)

# Status codes worth asking again about. 5xx only: the server is broken, not the request.
#
# 429 IS DELIBERATELY ABSENT, and the reason is forecast.solar. Its free tier allows about
# twelve requests an hour per IP, which an hourly planner with two panel groups can exhaust
# on its own; retrying inside a minute cannot clear a rate limit whose window is an hour, and
# every retry spends budget the NEXT run needs. Same logic as collector/efficiency.py's
# RETRY_CODES excluding the clock-skew code: only retry what a retry can fix.
HTTP_RETRY_STATUSES = (500, 502, 503, 504)


def scrubQuery(error, url):
    """The error text with `url`'s query string redacted.

    requests spells a connection failure as "Max retries exceeded with url: /api?..." - it
    quotes the path AND the query back at you. The ENTSOE url carries `securityToken=` in its
    query, so printing the raw exception would write the token into logs/plan_*.log on every
    network blip. Nothing before this printed exception text on that path, so the leak would
    have arrived WITH the retry logging, hidden inside a change about something else.

    Redacts every query string, not just the one that happens to hold a secret: a helper that
    has to be told which endpoints are sensitive gets it wrong the first time a new one is
    added.
    """
    query = urllib.parse.urlsplit(url).query
    text = str(error)
    return text.replace(query, "<redacted>") if query else text


def getWithRetries(url, what, timeout=HTTP_TIMEOUT, backoff=HTTP_RETRY_BACKOFF_S,
                   retryStatuses=HTTP_RETRY_STATUSES):
    """`requests.get`, retried through transient failures. `what` names the endpoint in logs.

    Returns the last Response, INCLUDING a non-200 one, so that call sites keep their existing
    `if response.status_code == 200:` shape and their existing error branches. Raises the last
    exception only when every attempt raised - a caller that already handles a dead endpoint
    keeps handling it, just later and less often.

    Retries transport failures (DNS, connection refused, timeout, reset) and `retryStatuses`.
    Nothing else: a 4xx is this request being wrong, and asking again with the same URL is
    just a slower way to get the same answer.
    """
    lastError = None
    for attempt in range(len(backoff) + 1):
        if attempt:
            delay = backoff[attempt - 1]
            print("WARNING: %s failed (%s); retrying in %ds (%d/%d)"
                  % (what, scrubQuery(lastError, url), delay, attempt, len(backoff)))
            time.sleep(delay)
        try:
            response = requests.get(url, timeout=timeout)
        except requests.RequestException as e:
            # Deliberately not `except Exception`: a KeyboardInterrupt or a SystemExit raised
            # mid-request must not be swallowed and retried three more times (CODE-REVIEW.md
            # B2 is the same hazard, found the hard way).
            lastError = e
            continue
        if response.status_code in retryStatuses and attempt < len(backoff):
            lastError = "HTTP %s" % response.status_code
            continue
        return response
    raise lastError
