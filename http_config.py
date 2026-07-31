"""Shared HTTP timeout policy (CODE-REVIEW.md E7).

(connect, read) seconds. A scheduled job that hangs forever is worse than one that
fails: nothing reports it, and the next run stacks up behind it. Applied to every
outbound call this project makes - forecast.solar, ENTSOE and EnergyZero from
Marstek-planning.py, and InfluxDB's query/write endpoints from influx_source.py.

Before this, Marstek-planning.py declared (10, 30) once and influx_source.py
separately hardcoded a bare 30 (connect and read together) at its two live call
sites - the same policy expressed twice, with no guarantee an edit to one would ever
reach the other.
"""
HTTP_TIMEOUT = (10, 30)
