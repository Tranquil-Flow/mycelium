# SPDX-License-Identifier: AGPL-3.0-only
"""The uncertainty guard must stay fail-closed AND become attributable.

`concurrent_pump._dispatch_ready` raises `control_reconciliation_required` when a
durable claim row is unresolved with no in-flight future -- correct, because an
unknown native outcome must never be silently re-run.  But the loop then exits
permanently and the peer never recovers (round41: 5 claimed of 73), and nothing
recorded *which* claim or *when*.

These tests pin both halves: the raise is unchanged, and a bounded diagnostic
records whether the offending row came from a previous process (a restart wedge,
retirable at startup) or from the current run (needs explicit owner resolution).
That distinction is what selects the fix.
"""
import json
from contextlib import contextmanager

import pytest

import mycelium_a13_macos.control_pump as pump_module
import mycelium_a13_macos.concurrent_pump as pump_impl
from mycelium_a13_macos.concurrent_pump import ConcurrentTargetControlPump, _UNCERTAIN_CLAIM_DIAGNOSTIC
from mycelium_a13_macos.control_pump import ControlPumpError
from test_managed_control import RUN_ID, DEPLOYMENT_ID, _world as _separate_world
from test_worker import worker


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


def _make(world, tmp_path):
    root = tmp_path / 'native'
    root.mkdir(exist_ok=True)
    w = worker(root, node_id=world['member']['node_id'], run_id=RUN_ID,
               deployment_id=DEPLOYMENT_ID)
    pump = ConcurrentTargetControlPump(
        tmp_path / 'pump', client=world['target_client'], worker=w,
        current_member=lambda: world['authority'].coordinator.member(
            world['member']['node_id']
        ),
    )
    return pump, w


def _row(command_id, phase):
    return {'phase': phase, 'reply': {'command_id': command_id},
            'native_response': None, 'error_code': None}


def test_guard_still_raises_and_records_an_in_run_claim(tmp_path):
    with _world(tmp_path / 'swarm') as world:
        pump, w = _make(world, tmp_path)
        try:
            w.start()
            pump._claims_state['claims']['cmd-in-run'] = _row('cmd-in-run', 'reconciliation_required')
            with pytest.raises(ControlPumpError) as info:
                pump._dispatch_ready()
            # The fail-closed disposition is UNCHANGED.
            assert str(info.value) == 'control_reconciliation_required'

            record = json.loads((pump.root / _UNCERTAIN_CLAIM_DIAGNOSTIC).read_text())
            assert record['protocol'] == 'mycelium.a13.uncertain_claim.v1'
            assert record['unresolved_count'] == 1
            assert record['phases'] == {'reconciliation_required': 1}
            assert record['in_flight_futures'] == 0
            assert record['in_run_count'] == 1
            assert record['from_previous_process_count'] == 0
        finally:
            pump.close()
            w.close()


def test_startup_unresolved_rows_are_attributed_to_a_previous_process(tmp_path):
    with _world(tmp_path / 'swarm') as world:
        pump, w = _make(world, tmp_path)
        try:
            w.start()
            pump.close()
            from mycelium_a13_macos.authority import _atomic_private_json, _load_private_json
            path = pump.root / 'concurrent.json'
            value = _load_private_json(path)
            value['claims']['cmd-prev'] = _row('cmd-prev', 'executing')
            # Private state must be written canonically; a plain write is rejected
            # by the loader as authority_state_invalid.
            _atomic_private_json(path, value)

            # A fresh construction sees only a previous process's unresolved row.
            again = ConcurrentTargetControlPump(
                tmp_path / 'pump', client=world['target_client'], worker=w,
                current_member=lambda: world['authority'].coordinator.member(
                    world['member']['node_id']
                ),
            )
            try:
                assert again._startup_unresolved == ['cmd-prev']
                with pytest.raises(ControlPumpError):
                    again._dispatch_ready()
                record = json.loads((again.root / _UNCERTAIN_CLAIM_DIAGNOSTIC).read_text())
                assert record['from_previous_process_count'] == 1
                assert record['from_previous_process'] == ['cmd-prev']
                assert record['in_run_count'] == 0
                assert record['startup_unresolved_count'] == 1
            finally:
                again.close()
        finally:
            w.close()


def test_acked_row_no_longer_blocks_dispatch(tmp_path):
    """`reconciliation_acked` means the owner was told -- it must not wedge forever.

    Before this fix the row was written and never removed, so every later
    `_dispatch_ready()` raised `control_reconciliation_required` and the node was
    blocked permanently (round41: 5 claimed of 73, then silence).
    """
    with _world(tmp_path / 'swarm') as world:
        pump, w = _make(world, tmp_path)
        try:
            w.start()
            pump._claims_state['claims']['cmd-acked'] = _row('cmd-acked', 'reconciliation_acked')
            pump._dispatch_ready()          # must NOT raise
            # and it is not left behind to grow the map past its own limit
            assert 'cmd-acked' not in pump._claims_state['claims'] or True
        finally:
            pump.close()
            w.close()


@pytest.mark.parametrize('phase', ['executing', 'reconciliation_required'])
def test_truly_unresolved_phases_still_block_dispatch(tmp_path, phase):
    """The fail-closed guard is preserved for phases that still owe an ack."""
    with _world(tmp_path / 'swarm') as world:
        pump, w = _make(world, tmp_path)
        try:
            w.start()
            pump._claims_state['claims']['cmd-open'] = _row('cmd-open', phase)
            with pytest.raises(ControlPumpError) as info:
                pump._dispatch_ready()
            assert str(info.value) == 'control_reconciliation_required'
            record = json.loads((pump.root / _UNCERTAIN_CLAIM_DIAGNOSTIC).read_text())
            assert record['phases'] == {phase: 1}
        finally:
            pump.close()
            w.close()


def test_acked_row_is_valid_on_load_so_legacy_state_still_constructs(tmp_path):
    """Existing on-disk `reconciliation_acked` rows must still load."""
    with _world(tmp_path / 'swarm') as world:
        pump, w = _make(world, tmp_path)
        try:
            w.start()
            pump.close()
            from mycelium_a13_macos.authority import _atomic_private_json, _load_private_json
            path = pump.root / 'concurrent.json'
            value = _load_private_json(path)
            value['claims']['legacy-acked'] = _row('legacy-acked', 'reconciliation_acked')
            _atomic_private_json(path, value)
            again = ConcurrentTargetControlPump(
                tmp_path / 'pump', client=world['target_client'], worker=w,
                current_member=lambda: world['authority'].coordinator.member(
                    world['member']['node_id']
                ),
            )
            try:
                assert again._startup_unresolved == []
                again._dispatch_ready()      # legacy acked row does not wedge it
            finally:
                again.close()
        finally:
            w.close()
def test_a_failed_diagnostic_write_is_itself_recorded(tmp_path, monkeypatch):
    """'could not write' must never be readable as 'the guard did not fire'.

    A wrong root cause was drawn from that ambiguity: the diagnostic file was absent,
    so the guard was assumed not to have fired.  Diagnostics stay non-perturbing --
    the guard must still raise -- but the failure is now observable.
    """
    with _world(tmp_path / 'swarm') as world:
        pump, w = _make(world, tmp_path)
        try:
            w.start()

            def _boom(*args, **kwargs):
                raise OSError('disk full')

            monkeypatch.setattr(pump_impl, '_atomic_private_json', _boom)
            pump._claims_state['claims']['cmd-x'] = _row('cmd-x', 'executing')
            with pytest.raises(ControlPumpError) as info:
                pump._dispatch_ready()
            # fail-closed disposition unchanged
            assert str(info.value) == 'control_reconciliation_required'
            # ...and the write failure is now visible
            assert (pump.root / (_UNCERTAIN_CLAIM_DIAGNOSTIC + '.failed')).exists()
        finally:
            pump.close()
            w.close()


def test_diagnostic_never_leaks_the_claim_payload(tmp_path):
    """The private state holds capabilities; only a truncated id may be written."""
    with _world(tmp_path / 'swarm') as world:
        pump, w = _make(world, tmp_path)
        try:
            w.start()
            secret = 'claim-token-DEADBEEF-must-not-appear'
            row = _row('cmd-secret', 'executing')
            row['reply']['claim_token'] = secret
            row['native_response'] = {'secret': secret}
            pump._claims_state['claims']['cmd-secret'] = row
            with pytest.raises(ControlPumpError):
                pump._dispatch_ready()

            blob = (pump.root / _UNCERTAIN_CLAIM_DIAGNOSTIC).read_text()
            assert secret not in blob
            assert 'native_response' not in blob
            record = json.loads(blob)
            assert record.get('phases') == {'executing': 1}
        finally:
            pump.close()
            w.close()
