# SPDX-License-Identifier: AGPL-3.0-only
"""Desk-only entry driver: real native decode method, fake two-stage compute.

This does not test the managed mailbox/iroh wire or authorize a device run.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from physical_inference_node import PhysicalNodeService, _CaptureSink
from mycelium_router.transports.iroh import IrohTransportError


CONTROL = {
    'deployment_id': 'deployment', 'deployment_epoch': 1,
    'qualification_digest': 'sha256:' + 'a' * 64,
    'request_attempt': 1, 'path_id': 'path', 'path_attempt': 1,
    'path_digest': 'sha256:' + 'b' * 64, 'topology_generation': 1,
    'command_id': 'command', 'cancellation_generation': 0,
    'publisher_generation': 1, 'absolute_deadline_ms': 100000,
}


class DeskNode:
    def __init__(self, *, fail_at=None, cancel_at=None, delay=0.001):
        self.node = object.__new__(PhysicalNodeService)
        n = self.node
        n.state = 'RUNNING'
        n._idle_gate = lambda: None
        n.command_timeout = 2
        n._control_lock = threading.RLock()
        n._request_controls = {'request': dict(CONTROL)}
        n._request_cleanup_receipts = {}
        n._sinks = {'request': _CaptureSink()}
        from mycelium_qualification.signing import generate_ed25519_signer
        n.signer = generate_ed25519_signer(endpoint_id='desk-token-path')
        n._identity = lambda: {'run_id': 'desk', 'node_id': 'entry'}
        n.transport = None
        self.now = 0.0
        self.cancelled = False
        self.status = 'DECODING'
        self.trace = []
        self.rounds = 0
        self.control_commands = []
        self.receipts = []
        self.delay = delay
        self.fail_at = fail_at
        self.cancel_at = cancel_at
        n.router = SimpleNamespace(decode_one_distributed=self.decode,
                                   request_status=lambda _: self.status)

    def decode(self, request_id):
        self.rounds += 1
        if self.rounds == self.fail_at:
            raise IrohTransportError('delivery_deadline_exceeded')
        self.trace.extend([(self.rounds, 'entry'), (self.rounds, 'tail')])
        self.now += self.delay
        self.node._sinks[request_id].emit(self.rounds - 1, self.rounds)
        if self.rounds == self.cancel_at:
            self.cancelled = True
        return True

    def run(self, **overrides):
        # Start has already been authenticated/admitted by the caller. This
        # adapter is NOT registered in the managed command grammar.
        self.control_commands.append('infer_start')
        args = dict(request_id='request', control=dict(CONTROL), max_new_tokens=6,
                    stop_token_ids=(), deadline_monotonic_s=10.0,
                    run_deadline_monotonic_s=1800.0, publish=self.receipts.append,
                    cancelled=lambda: self.cancelled, clock=lambda: self.now,
                    enabled=True, batch_tokens=2, batch_seconds=0.05)
        args.update(overrides)
        return self.node.drive_entry_token_path(**args)

    def legacy(self, count=6):
        self.control_commands.append('infer_start')
        for _ in range(count):
            self.control_commands.append('infer_decode')
            self.node._infer_decode({'request_id': 'request', 'count': 1,
                                     'control': dict(CONTROL)})


def token_ids(node):
    return tuple(t for receipt in node.receipts for t in receipt.token_ids)


def test_one_inference_command_instead_of_start_plus_n_and_same_execution():
    legacy, local = DeskNode(), DeskNode()
    legacy.legacy()
    result = local.run()
    assert len(legacy.control_commands) == 7
    assert local.control_commands == ['infer_start']
    assert local.rounds == legacy.rounds == 6
    assert local.trace == legacy.trace  # fake per-stage execution evidence
    assert token_ids(local) == tuple(legacy.node._sinks['request'].token_ids)
    assert result.status == 'COMPLETED'
    assert [r.start_index for r in local.receipts] == [0, 2, 4]
    assert [r.sequence for r in local.receipts] == [0, 1, 2]
    assert local.receipts[-1].status == 'COMPLETED'


def test_default_off_refuses_before_compute_or_receipts():
    d = DeskNode()
    with pytest.raises(RuntimeError, match='token_path_disabled'):
        d.run(enabled=False)
    assert not d.trace and not d.receipts


def test_cancel_midstream_drops_unpublished_and_overtaken_token():
    d = DeskNode(cancel_at=3)
    result = d.run()
    assert result.status == 'CANCELLED'
    assert d.rounds == 3
    assert token_ids(d) == (1, 2)
    assert d.receipts[-1].status == 'CANCELLED'


def test_cancel_before_loop_runs_no_decode():
    d = DeskNode()
    d.cancelled = True
    assert d.run().status == 'CANCELLED'
    assert d.rounds == 0 and token_ids(d) == ()


@pytest.mark.parametrize('field', ['deadline_monotonic_s', 'run_deadline_monotonic_s'])
def test_deadline_and_process_ceiling_stop_before_next_round(field):
    d = DeskNode(delay=0.01)
    with pytest.raises(TimeoutError, match='command_deadline_exceeded'):
        d.run(**{field: 0.025})
    assert d.rounds == 3
    assert token_ids(d) == (1, 2)


def test_expired_grant_has_no_side_effects():
    d = DeskNode()
    with pytest.raises(TimeoutError, match='command_deadline_exceeded'):
        d.run(deadline_monotonic_s=0)
    assert d.rounds == 0 and d.receipts == []


def test_stage_loss_keeps_existing_exception_and_code():
    d = DeskNode(fail_at=3)
    with pytest.raises(IrohTransportError) as new:
        d.run()
    legacy = DeskNode(fail_at=3)
    with pytest.raises(IrohTransportError) as old:
        legacy.legacy()
    assert new.value.code == old.value.code == 'delivery_deadline_exceeded'
    assert token_ids(d) == (1, 2)
    assert not any(r.status == 'COMPLETED' for r in d.receipts)


def test_stop_token_not_published_and_no_next_decode():
    d = DeskNode()
    assert d.run(stop_token_ids=(3,)).status == 'COMPLETED'
    assert d.rounds == 3 and token_ids(d) == (1, 2)


def test_time_batch_is_checked_each_completed_round():
    d = DeskNode(delay=0.03)
    d.run(batch_tokens=8)
    assert [len(r.token_ids) for r in d.receipts] == [2, 2, 2]


def test_receipt_backpressure_failure_stops_compute():
    d = DeskNode()
    def blocked(_):
        raise RuntimeError('receipt_unavailable')
    with pytest.raises(RuntimeError, match='receipt_unavailable'):
        d.run(publish=blocked)
    assert d.rounds == 2


def test_generation_change_still_fails_native_decode_fence():
    d = DeskNode()
    d.node._request_controls['request']['publisher_generation'] = 2
    with pytest.raises(RuntimeError, match='stale_infer_decode_generation'):
        d.run()
    assert not d.trace


@pytest.mark.parametrize('kwargs', [
    {'max_new_tokens': True}, {'max_new_tokens': 0},
    {'deadline_monotonic_s': float('nan')},
    {'run_deadline_monotonic_s': float('inf')},
    {'batch_tokens': 0}, {'batch_seconds': 0}, {'stop_token_ids': (True,)},
])
def test_bad_local_grant_fails_before_decode(kwargs):
    d = DeskNode()
    with pytest.raises(ValueError, match='token_path_'):
        d.run(**kwargs)
    assert not d.trace and not d.receipts


def test_eos_completion_without_new_token_is_not_a_stall():
    d = DeskNode()
    def eos(_):
        d.status = 'COMPLETED'
        return True
    d.node.router.decode_one_distributed = eos
    assert d.run().status == 'COMPLETED'
    assert token_ids(d) == ()


def test_failed_snapshot_is_not_empty_success():
    d = DeskNode()
    d.status = 'FAILED'
    with pytest.raises(RuntimeError, match='token_path_inference_failed'):
        d.run()
    assert not d.receipts


def test_no_progress_is_not_an_infinite_loop():
    d = DeskNode()
    d.node.router.decode_one_distributed = lambda _: False
    with pytest.raises(RuntimeError, match='route_decode_stalled'):
        d.run()
    assert not d.receipts


def test_prefill_output_is_bounded_into_batches():
    d = DeskNode()
    for index in range(6):
        d.node._sinks['request'].emit(index, index + 1)
    assert d.run().status == 'COMPLETED'
    assert d.rounds == 0
    assert [len(r.token_ids) for r in d.receipts] == [2, 2, 2]


def test_stop_in_prefill_never_dispatches_decode():
    d = DeskNode()
    for index in range(6):
        d.node._sinks['request'].emit(index, index + 1)
    d.run(stop_token_ids=(3,))
    assert d.rounds == 0 and token_ids(d) == (1, 2)


def test_cancellation_during_batch_publication_stops_next_batch():
    d = DeskNode()
    for index in range(6):
        d.node._sinks['request'].emit(index, index + 1)
    def publish(batch):
        d.receipts.append(batch)
        d.cancelled = True
    result = d.run(publish=publish)
    assert result.status == 'CANCELLED'
    assert token_ids(d) == (1, 2)


def test_mutated_output_prefix_is_rejected_before_receipt():
    d = DeskNode()
    original = d.decode
    def mutated(request_id):
        result = original(request_id)
        if d.rounds == 2:
            d.node._sinks[request_id].token_ids[0] = 99
        return result
    d.node.router.decode_one_distributed = mutated
    with pytest.raises(RuntimeError, match='token_path_output_invalid'):
        d.run()
    assert not d.receipts


def test_prefill_first_token_is_published_before_any_decode():
    d = DeskNode()
    d.node._sinks['request'].emit(0, 100)
    rounds_at_publication = []
    def publish(batch):
        d.receipts.append(batch)
        rounds_at_publication.append(d.rounds)
    # Fake decode indexes begin after the existing prefill token.
    d.rounds = 1
    d.run(publish=publish)
    assert rounds_at_publication[0] == 1
    assert d.receipts[0].token_ids == (100,)


def test_publication_overrunning_deadline_cannot_return_success():
    d = DeskNode()
    def delayed(batch):
        d.receipts.append(batch)
        d.now = 20.0
    with pytest.raises(TimeoutError, match='command_deadline_exceeded'):
        d.run(max_new_tokens=2, publish=delayed)
    assert d.rounds == 2


def test_adapter_is_not_a_remotely_registered_command():
    from mycelium_a13_macos.worker import _OPERATION_PAYLOAD_FIELDS
    assert 'drive_entry_token_path' not in _OPERATION_PAYLOAD_FIELDS
    assert all('token_path' not in fields
               for fields in _OPERATION_PAYLOAD_FIELDS['infer_start'])
