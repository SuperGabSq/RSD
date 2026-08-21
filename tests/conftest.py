"""Fixtures shared across phases.

The ``simulator`` fixture lives here rather than in either phase directory because both
Phase 1 (which tests the simulator) and Phase 3 (which streams it through the backend)
need it, and a fixture that has to be imported is a fixture that will drift.
"""

from __future__ import annotations

import pytest

from tests.support.simulator import Simulator, start_simulator


@pytest.fixture
def simulator():
    """Start one or more simulators; every one is torn down after the test.

    Returns a factory rather than an instance so a test can pass fault-injection flags,
    or start two simulators at different rates, without a second fixture.
    """
    started: list[Simulator] = []

    def start(*extra_args: str) -> Simulator:
        sim = start_simulator(*extra_args)
        started.append(sim)
        return sim

    yield start
    for sim in started:
        sim.stop()
