"""http_config.py is the single source for the outbound HTTP timeout policy
(CODE-REVIEW.md E7). Before this, Marstek-planning.py declared (10, 30) once and
influx_source.py separately hardcoded a bare 30 at its two live call sites.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import http_config
import influx_source as ix


def test_timeout_is_a_connect_read_pair():
    assert http_config.HTTP_TIMEOUT == (10, 30)


def test_influx_source_reads_the_shared_timeout():
    assert ix.http_config.HTTP_TIMEOUT is http_config.HTTP_TIMEOUT


def test_marstek_planning_reads_the_shared_timeout(planner):
    assert planner.HTTP_TIMEOUT == http_config.HTTP_TIMEOUT
