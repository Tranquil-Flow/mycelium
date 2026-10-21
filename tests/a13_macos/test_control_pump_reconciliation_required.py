# SPDX-License-Identifier: AGPL-3.0-only
"""A `reconciliation_required` delivery must not wedge the target control loop.

Round 54 physical run `a13-qwen-20260915T210439Z` (preview.57).  The peer accepted
work (`target WORK_ACCEPTED`), then its control pump stopped claiming forever:

    target  health  reconciliation_required  21:07:48.447
    target  health  reconciliation_required  21:07:49.641
    target  health  completed                21:07:50.996
    target  health  claimed (never updated)  21:07:52.671   <- last claim, ever
    target  health  expired  x117            21:08:50 -> 21:09:55
    target  stop    expired                  21:09:55.653

while the owner's own node completed 120/120 `health` through 21:08:55.  The peer's
own record for that run is
`runtime-control-failure.private.json = {state: blocked, error_code:
managed_control_message_expired, exception_type: ManagedControlError}` — the same
one-sided shape, and the same death class, that `test_control_pump_expired_outbound_parent`
documents from round 38: *"`step()` raised, and `managed_runtime._serve` catches any
exception and exits the control loop for good.  The node was then permanently
`blocked`."*

The candidate site is a branch that fires on the FIRST reconciliation delivery:

    self._state['outbound'] = None                       # 137, just cleared
    ...
    pending = {'phase': 'ready', ...}
    if reply['delivery_kind'] == 'reconciliation_required':
        pending['phase'] = 'reconciliation_required'      # 141, just set
    self._state['pending'] = pending; self._save()        # 142
    if pending['phase'] in {'executing', 'reconciliation_required'} \
            and self._state['outbound'] is None:
        raise ControlPumpError('control_reconciliation_required')   # 143-146

The guard is meant for a pending command resumed from durable state, where an unknown
native outcome genuinely cannot be re-run.  But `pending` here was created in this same
step, `outbound` was cleared four lines earlier, and `step()` already knows how to
answer the mailbox honestly at line 162 (`disposition = 'reconciliation_required'`) —
without replaying any native work.  Raising instead skips that ack, leaves the mailbox
row unreconciled, and (per `managed_control.py:1185-1210`, which delivers
`unresolved[0]` ahead of new work) starves every later command.

These tests pin both halves: the reconciliation delivery must not kill the loop, and a
genuinely resumed unknown outcome must still fail closed.
"""

import pytest

import mycelium_a13_macos.control_pump as pump_module
from mycelium_a13_macos.control_pump import ControlPumpError, TargetControlPump
from test_managed_control import RUN_ID, DEPLOYMENT_ID, _world as _separate_world
from test_worker import worker

from contextlib import contextmanager


@contextmanager
def _world(path):
    with _separate_world(path) as world:
        authority = world['authority']
        common = dict(
            base_url=authority.seed_server.base_url,
            trusted_owner_key_record=world['owner_signer'].public_key_record(),
            swarm_id=authority.coordinator.swarm_id,
            owner_node_id=authority.coordinator.seed_node_id,
            ca_certificate=authority.seed_server.material.ca_certificate,
        )
        world['target_client'] = pump_module.ManagedControlClient.for_local_tls(
            signer=world['target_signer'], **common
        )
        yield world


def _pump(world, tmp_path):
    root = tmp_path / 'native'
    root.mkdir()
    w = worker(root, node_id=world['member']['node_id'], run_id=RUN_ID,
               deployment_id=DEPLOYMENT_ID)
    pump = TargetControlPump(
        tmp_path / 'pump', client=world['target_client'], worker=w,
        current_member=lambda: world['authority'].coordinator.member(
            world['member']['node_id']
        ),
    )
    return pump, w


def _reconciliation_reply(command='health'):
    """What the mailbox returns for an unresolved claim, before any body is sent."""

    return {
        'delivery_kind': 'reconciliation_required',
        'claim_token': 'claim-token-reconciliation',
        'command_envelope': {
            'statement': {
                'native_command': {
                    'command_id': 'command-reconciliation',
                    'command': command,
                    'payload': {},
                    'run_id': RUN_ID,
                    'deployment_id': DEPLOYMENT_ID,
                }
            }
        },
    }


def test_a_reconciliation_delivery_does_not_kill_the_pump(tmp_path, monkeypatch):
    """RED before the fix: the first reconciliation delivery must not raise.

    `managed_runtime._serve` exits the control loop on ANY exception from `step()`, so
    a raise here is terminal for the node -- and it is what the round-54 mailbox
    timeline shows.
    """

    with _world(tmp_path / 'swarm') as world:
        pump, w = _pump(world, tmp_path)
        try:
            w.start()
            monkeypatch.setattr(pump_module, 'replay',
                                lambda client, prepared, *, target: _reconciliation_reply())
            result = pump.step()

            # Recoverable, and it must say so rather than dying.
            assert result['state'] == 'reconciliation_required'
            assert result['command_id'] == 'command-reconciliation'
            # Nothing native may be replayed to satisfy a reconciliation.
            assert result['native_response'] is None
        finally:
            pump.close()
            w.close()


def test_a_resumed_unknown_outcome_still_fails_closed(tmp_path, monkeypatch):
    """The fail-closed half, which must NOT change.

    A pending command loaded from durable state in `executing` with no durable outbound
    is a genuinely unknown native outcome: it must stay unsafe to re-run.
    """

    with _world(tmp_path / 'swarm') as world:
        pump, w = _pump(world, tmp_path)
        try:
            w.start()
            pump._state['pending'] = {
                'phase': 'executing',
                'reply': _reconciliation_reply(),
                'native_response': None,
                'error_code': None,
            }
            pump._state['outbound'] = None
            pump._save()
            monkeypatch.setattr(pump_module, 'replay',
                                lambda client, prepared, *, target: _reconciliation_reply())

            with pytest.raises(ControlPumpError, match='control_reconciliation_required'):
                pump.step()
        finally:
            pump.close()
            w.close()
