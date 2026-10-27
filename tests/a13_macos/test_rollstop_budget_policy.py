# SPDX-License-Identifier: AGPL-3.0-only
"""R2/R3 policy counterexamples, not a waiver or positive admission claim.

Actual protocol waits cannot fit the fixed margin. Check that a strict validator
REFUSES them, expose each inequality, and simulate measured stop latency. The
single observed sample is not a p99. No wall/margin/TTL is enlarged for this test.
"""
from dataclasses import dataclass
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from mycelium_a13_macos.demo_chain import AppDemoChain
from mycelium_a13_macos.owner_run import OwnerRun, RunContext
from mycelium_a13_macos.workflow import ManagedServingWorkflow
from mycelium_a13_macos.worker import _CLEANUP_RESERVE_SECONDS
from test_execution_ceiling import Clock
from test_rollover_stop_budget import timed_run

PROFILE = json.loads((Path(__file__).parent / 'fixtures/rollstop-latency-profile.json').read_text())


@dataclass(frozen=True)
class RestartBudgetSnapshot:
    """One typed test table, all operational values read from production."""
    wall: float
    route_close_wait_sum: float
    stop_allowance: float
    effective_stop: float
    configured_margin: float
    effective_margin: float
    ceiling_lead: float
    safety: float
    start_phase_allowance_sum: float

    def violations(self, profile):
        factor = profile['minimum_multiplier']
        stop_floor = factor * profile['phases']['route_stop']['observed_seconds']
        start_floor = factor * profile['phases']['compile_to_serving']['observed_seconds']
        checks = {
            'stop_allowance_below_close_wait_sum': self.stop_allowance >= self.route_close_wait_sum,
            'effective_stop_below_close_wait_sum': self.effective_stop >= self.route_close_wait_sum,
            'margin_below_stop_plus_safety': self.effective_margin >= self.stop_allowance + self.safety,
            'ceiling_not_before_rollover': self.ceiling_lead < self.effective_margin,
            'stop_below_2x_observed': self.effective_stop >= stop_floor,
            'start_below_2x_observed': self.start_phase_allowance_sum >= start_floor,
            # Adopted full-chain rule includes restart. New-run worker clocks
            # do not grant continuity on the old route during this outage.
            'margin_below_2x_measured_restart': self.effective_margin >= stop_floor + start_floor + self.safety,
            'p99_profile_missing': profile['p99_seconds'] is not None,
        }
        return tuple(code for code, good in checks.items() if not good)

    def require_coherent(self, profile):
        failures = self.violations(profile)
        if failures:
            raise ValueError(','.join(failures))


def snapshot(wall, monkeypatch):
    from mycelium_live import route, router_port, supervisor
    from physical_inference_qualification import STAGE_CLEANUP_TIMEOUT_SECONDS
    workflow = ManagedServingWorkflow(authority=None, controller=SimpleNamespace(peers=('a','b')), launch=None)
    chain = AppDemoChain(SimpleNamespace(_chain_workflow=workflow))
    clock = Clock()
    chain._monotonic = clock
    ctx = RunContext(OwnerRun(chain, clock=clock), 'run', 'deployment', 'rollover')
    ctx.state.update(execution_clock_started_at=0, work_limits={'max_wall_seconds': wall})
    margin = min(max(chain.EXECUTION_CEILING_LEAD_SECONDS, chain.budgets['rollover_margin']), wall / 2)
    clock.now = wall - margin
    assert chain.rollover_due(ctx)
    clock.now -= .001
    assert not chain.rollover_due(ctx)
    clock.now += .001
    expected_close = (supervisor.REGISTRY_SERVER_POLL_SECONDS + router_port.ROUTER_CLOSE_TIMEOUT_SECONDS
        + route._MAX_CANCELLATION_AND_CLEANUP_SECONDS + route.ROUTE_CLOSE_MONITOR_JOIN_SECONDS
        + 2 * (2 * route.NODE_SESSION_TIMEOUT_SECONDS + STAGE_CLEANUP_TIMEOUT_SECONDS))
    allowance = workflow.close_wait_seconds()
    assert allowance == expected_close
    return RestartBudgetSnapshot(wall, expected_close, allowance, chain._stop_deadline(ctx) - clock(),
        chain.budgets['rollover_margin'], margin, chain.EXECUTION_CEILING_LEAD_SECONDS,
        _CLEANUP_RESERVE_SECONDS,
        sum(chain.budgets[p] for p in ('compile', 'slices', 'publish', 'targets', 'serving')))


@pytest.mark.parametrize('wall', [60, 600, 1800, 3600])
def test_real_default_restart_chain_is_explicitly_not_policy_qualified(wall, monkeypatch):
    row = snapshot(wall, monkeypatch)
    expected = {'effective_stop_below_close_wait_sum', 'margin_below_stop_plus_safety',
                'margin_below_2x_measured_restart', 'p99_profile_missing'}
    if wall == 60:
        expected |= {'ceiling_not_before_rollover', 'stop_below_2x_observed'}
    assert set(row.violations(PROFILE)) == expected
    # A green counterexample test means the admission check REJECTED, not that
    # the candidate satisfies R2/R3. A release must not treat this as a waiver.
    with pytest.raises(ValueError, match='effective_stop_below_close_wait_sum'):
        row.require_coherent(PROFILE)
    assert row.effective_stop == min(row.stop_allowance, row.effective_margin - row.safety)
    assert row.effective_stop <= wall - row.safety


@pytest.mark.parametrize('scale', PROFILE['loaded_simulation_multipliers'])
def test_measured_loaded_stop_profile_rolls_over_after_real_proof_seam(timed_run, scale):
    t = timed_run
    thread = t.install(PROFILE['phases']['route_stop']['observed_seconds'] * scale)
    status = t.rollover()
    assert status['state'] == 'serving', status
    assert thread.done and status['rollover']['completed'] == 1
    assert t.clock() == pytest.approx(thread.started + 33 * scale)


def test_60_second_fallback_cannot_grant_2x_measured_stop(timed_run):
    t = timed_run
    for limits in [t.old.state['work_limits'], *[r['work_limits'] for r in t.old.state['work_nodes'].values()]]:
        limits['max_wall_seconds'] = 60
    thread = t.install(PROFILE['phases']['route_stop']['observed_seconds'] * PROFILE['minimum_multiplier'])
    status = t.rollover(at=30)
    assert status['error_code'] == 'rollover_cleanup_unproven'
    assert status['cleanup_unproven']['stop_error'] == 'rebuild_stop_timeout'
    assert not thread.done
    assert t.clock() == t.old.state['execution_clock_started_at'] + 60 - _CLEANUP_RESERVE_SECONDS
