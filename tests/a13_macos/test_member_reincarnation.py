# SPDX-License-Identifier: AGPL-3.0-only
"""Routine relay rotation re-admits the SAME node with a NEWER incarnation.

Round 64r1, run `a13-qwen-20260916T195658Z`. The driver's own record shows the relay
rotating mid-run (`relay_rotated`, `peer_origin_known: false`, reconnect attempt 1
failing `seed_http_unreachable` for 8.8 s and attempt 2 connecting after 18.3 s). The
node then re-admitted: the laptop's `membership.state.json` reached
`membership_generation: 6` with `restart_count: 5`, and the mailbox carried rows for
both the old incarnation (`gen 5`, `...r4`) and the new one (`gen 6`, `...r5`).

The durable binding, however, is captured ONCE -- in `ManagedTargetRuntime.__init__`,
`control_pump.__init__` and each persisted pump state file -- and every later check
compared it with plain equality:

    _binding_from_member(member, run_id, deployment_id) != self._binding

`_binding_from_member` includes `membership_generation` AND `member_incarnation`, so
the node's own re-admission read as "stale". `ManagedTargetRuntime._membership_current`
then returned False, the worker's guard raised `worker_membership_not_current`
(worker.py:589) and closed the native worker for good, and `_serve` died on the same
check -- one second before the run-level `configure` became unknowable.

These tests pin the fix: a newer incarnation of the SAME member is a re-admission to
accept and rebind, never a reason to stop. A different member, or an OLDER generation,
must still fail closed (see `test_revocation_stops_pump_and_process_preserves_stage` in
test_managed_runtime.py for the revocation case, which this change must not weaken).
"""

import time

from mycelium_a13_macos import managed_control as mc

from test_control_pump import _world
from test_managed_control import RUN_ID, DEPLOYMENT_ID
from test_managed_runtime import runtime


def _newer(member, *, generations=1, incarnation_suffix='.r9'):
    """The same member, re-admitted: a later generation and a new incarnation."""

    return {
        **dict(member),
        'generation': member['generation'] + generations,
        'incarnation': f"{member['incarnation']}{incarnation_suffix}",
        'lease_expires_at': time.time() + 600,
    }


def test_a_newer_incarnation_keeps_the_runtime_and_worker_alive(tmp_path):
    """RED before the fix: the node killed its own worker on its own re-admission."""

    with _world(tmp_path / 'world') as world:
        node_id = world['member']['node_id']
        current = [world['authority'].coordinator.member(node_id)]
        target = runtime(tmp_path / 'target', world, current_member=lambda: current[0])
        try:
            assert target.start()['state'] == 'running'

            current[0] = _newer(current[0])

            # The node is still ours: same node, same run and deployment.
            assert target._membership_current() is True
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                assert target.status()['state'] == 'running', target.status()
                assert target.worker.status()['running'] is True
                time.sleep(.05)
        finally:
            target.close()


def test_an_older_generation_still_fails_closed(tmp_path):
    """A rewind must never be accepted as a re-admission."""

    with _world(tmp_path / 'world') as world:
        node_id = world['member']['node_id']
        member = world['authority'].coordinator.member(node_id)
        current = [_newer(member, generations=3, incarnation_suffix='.newer')]
        target = runtime(tmp_path / 'target', world, current_member=lambda: current[0])
        try:
            assert target.start()['state'] == 'running'

            # An older incarnation of the same node is not a re-admission.
            current[0] = member

            assert target._membership_current() is False
        finally:
            target.close()


def test_binding_advance_is_narrow():
    """The predicate accepts only a newer generation of an otherwise identical member."""

    previous = {
        'target_node_id': 'node-1',
        'target_endpoint_id': 'endpoint-1',
        'target_key_digest': 'sha256:' + 'a' * 64,
        'membership_generation': 5,
        'member_incarnation': 'inc-1.r1',
        'run_id': RUN_ID,
        'deployment_id': DEPLOYMENT_ID,
    }
    newer = {**previous, 'membership_generation': 6, 'member_incarnation': 'inc-1.r2'}
    assert mc._binding_advanced(previous, newer) is True
    # Same generation is a no-op comparison, not an advance.
    assert mc._binding_advanced(previous, {**previous, 'member_incarnation': 'inc-1.r2'}) is False
    # A rewind never advances.
    assert mc._binding_advanced(newer, previous) is False
    # A different identity is never the same member.
    for field in ('target_node_id', 'target_endpoint_id', 'target_key_digest', 'run_id', 'deployment_id'):
        other = {**newer, field: 'different'}
        assert mc._binding_advanced(previous, other) is False, field
