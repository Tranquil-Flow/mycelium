# SPDX-License-Identifier: AGPL-3.0-only
"""Characterize the real workflow's retry cost with a fake clock, not hardware."""
import time

import pytest

from test_assignment_planning import enrolled, seed  # noqa: F401
from test_workflow_observations_shipping import _prepared_plan
from test_bootid_bounded_observation_wait import _bounded_wait, _prepare
from mycelium_a13_macos.managed_observations import ManagedObservationsError


@pytest.mark.parametrize('attempt_seconds', [0.0, 60.0])
def test_compile_boot_observation_wait_profile(tmp_path, enrolled, monkeypatch, record_property, attempt_seconds):
    authority, nodes, prepared, plan, roots, document = _prepared_plan(tmp_path, enrolled)
    clock = _bounded_wait(monkeypatch)
    attempts = []

    def unavailable(*args, **kwargs):
        attempts.append(clock['now'])
        # 60 s models one 30 s hello timeout followed by its 30 s close/stop.
        # It is a fixture cost, NOT a measurement of either installed Mac.
        clock['now'] += attempt_seconds
        raise ManagedObservationsError('managed_control_response_timeout')

    monkeypatch.setattr('mycelium_a13_macos.managed_observations.collect_managed_hello_observations', unavailable)
    started = time.monotonic()
    channel, workflow = _prepare(authority, prepared, plan, roots)
    try:
        assert len(attempts) > 1
        assert clock['now'] >= 180.0
        assert len(clock['sleeps']) == len(attempts) - 1
        record_property('simulated_elapsed_seconds', clock['now'])
        record_property('attempt_count', len(attempts))
        record_property('backoff_seconds', clock['sleeps'])
        record_property('fixture_wall_seconds', time.monotonic() - started)
        assert workflow is not None  # the exhausted observation remains inert
    finally:
        channel.close()
