"""Shared fixtures for tests that call into planner.py's functions directly
(as opposed to test_golden_plan.py, which runs the real CLI end to end)."""
import importlib.util
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def _loadPlanner():
    # Loaded by path, same reason fit_pv_elevation.py does it: the filename has a
    # hyphen, so it cannot be imported by name.
    spec = importlib.util.spec_from_file_location(
        "planner_under_test", os.path.join(REPO, "planner.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Only ever set by processCLarguments(), which a unit test does not run. Read by
    # LPoptimization()'s and calcTerminalReserveWh()'s "outputMode or debug" prints.
    mod.debug = False
    mod.outputMode = False
    return mod


@pytest.fixture
def planner():
    # Function-scoped, not module-scoped: the module under test carries a lot of
    # mutable global state (priceList, initialCharge, ...), and a fresh exec_module()
    # per test is cheap enough (a fraction of a second) that it isn't worth risking
    # one test's globals leaking into the next.
    return _loadPlanner()
