# SPDX-License-Identifier: AGPL-3.0-only
"""Real progress/control transport composition; native compute is controlled."""
import sys
import threading
import time
import traceback
from types import SimpleNamespace

from mycelium_live.token_path_owner import OwnerTokenPath
from mycelium_node.token_progress import ReceiptInbox
from mycelium_router.token_path import TokenBatch
from mycelium_router.token_path_authority import ReceiptCursor, ReceiptJournal
from test_inflight_route_stop import stack


def progress_transport(world, route, worker):
    signer = world['target_signer']
    worker.signer = signer
    route._endpoints[worker.node_id] = {'endpoint_id': signer.endpoint_id}
    route._verification_keys[worker.node_id] = signer.public_key_record()
    member = world['member']
    pins = dict(run_id=worker.run_id, requester_id='desk-requester',
        owner_endpoint_id=world['owner_signer'].endpoint_id,
        owner_key_digest=world['owner_signer'].verification_key_digest,
        entry_node_id=worker.node_id, entry_endpoint_id=signer.endpoint_id,
        entry_key_digest=signer.verification_key_digest,
        participants=[dict(node_id=worker.node_id, endpoint_id=signer.endpoint_id,
                           generation=member['generation'])],
        model_digest=route._graph.manifest_digest)
    attempts, inboxes, journals = {}, {}, {}
    sealed = []
    owner = OwnerTokenPath(enabled=True, signer=world['owner_signer'], scope_pins=pins,
        entry_key=signer.public_key_record(), mailbox=world['mailbox'], seal=sealed.append,
        current=lambda: not route._closed, capable=lambda n: n == worker.node_id,
        max_new_tokens=65536, stop_token_ids=(),
        process_deadline_monotonic_s=time.monotonic()+60,
        lease_deadline_monotonic_s=time.monotonic()+60, clock_uncertainty_ms=100)
    owner.required = True
    original_prepare = owner.prepare
    def prepare(**kwargs):
        attempt = original_prepare(**kwargs)
        attempts[kwargs['command_id']] = attempt
        return attempt
    owner.prepare = prepare
    route._token_path_owner = owner
    def cursor(command):
        return ReceiptCursor(grant=attempts[command['command_id']].grant,
                             entry_key=signer.public_key_record())
    def open_progress(*, command_id, cursor, deadline_monotonic_s):
        inbox = ReceiptInbox(cursor=cursor, deadline_monotonic_s=deadline_monotonic_s)
        inboxes[command_id] = inbox
        journals[command_id] = ReceiptJournal(grant=cursor.grant, signer=signer)
        return inbox
    worker.open_progress = open_progress
    worker.close_progress = lambda command_id: inboxes[command_id].close()
    worker.send_before = lambda **kwargs: {'ok': True}  # native ACK only
    def publish(command_id, *, tokens, status):
        journal = journals[command_id]
        if status == 'DECODING':
            receipt = journal.append(TokenBatch(0, 0, tuple(tokens), status))
        else:
            # Match the real native cancellation path: it retires generation 0
            # and returns an ordinary refusal, not a progress terminal after
            # the owner has closed its receipt stream.
            raise RuntimeError('request_cancelled')
        inboxes[command_id].feed(receipt)
        return dict(status=status, token_count=13, token_grant_digest=attempts[command_id].grant.digest)
    worker.token_owner = SimpleNamespace(publish=publish)
    original_send = worker.send
    def send(**kwargs):
        if kwargs['command'] == 'infer_cancel_wait':
            worker.release.set()
        try:
            return original_send(**kwargs)
        except RuntimeError as error:
            if kwargs['command'] != 'infer_start' or str(error) != 'request_cancelled':
                raise
            from mycelium_a13_macos.managed_control import CONTROL_COMMAND_PROTOCOL
            inboxes[kwargs['command_id']].finish('token_progress_missing_terminal')
            return dict(protocol=CONTROL_COMMAND_PROTOCOL, command_id=kwargs['command_id'],
                        node_id=worker.node_id, ok=False, route_ready=False,
                        error={'code': 'request_cancelled'})
    worker.send = send
    worker.progress_sealed = sealed
    worker.progress_inboxes = inboxes
    worker.progress_journals = journals
    worker.progress_owner = owner
    return cursor


def native_progress_transport(world, route, worker):
    from pathlib import Path
    from mycelium_node.token_admission import EntryAdmissionPolicy
    from physical_inference_node import PhysicalNodeService, NodeCommandError, _CaptureSink, _response
    from tests.physical_qualification.test_cancel_phase_diagnostics import _Runtime, _Transport

    cursor = progress_transport(world, route, worker)
    n = PhysicalNodeService(run_id=worker.run_id, deployment_id=worker.deployment_id,
        node_id=worker.node_id, artifact_root=world['database'].parent,
        socket_root=world['database'].parent/'socket', sidecar_binary=Path('/usr/bin/false'),
        sidecar_local_only=True, command_timeout=10)
    n.state = 'RUNNING'
    n.graph = route._graph
    n.signer, n.endpoint_id = worker.signer, worker.signer.endpoint_id
    n.host_id, n.process_id = 'desk-host', 123
    n._idle_gate = lambda: None
    n._host_resources = lambda: {}
    n.runtime, n.transport = _Runtime(), _Transport()
    n.runtime.cancel = lambda _: worker.release.set()
    n.router = SimpleNamespace(request_status=lambda _: 'CANCELLED' if worker.release.is_set() else 'DECODING',
        cancel_local=lambda _: True)
    def prefill(payload):
        sink = n._sinks.setdefault(payload['request']['request_id'], _CaptureSink())
        for index in range(13):
            sink.emit(index, 101+index)
    def decode(_):
        worker.entered.set()
        assert worker.release.wait(15)
        return False
    n._infer_start_legacy = prefill  # compute only
    n.router.decode_one_distributed = decode
    p = worker.progress_owner
    n._token_path_admission = EntryAdmissionPolicy(scope_pins=p.pins,
        owner_key=p.signer.public_key_record(), max_new_tokens=65536, stop_token_ids=(),
        process_deadline_monotonic_s=p.deadline, lease_deadline_monotonic_s=p.deadline,
        clock_uncertainty_ms=100)
    original_send = worker.send
    def send(*, command_id, command, payload, **kwargs):
        if command not in {'bind_request_control', 'infer_start', 'infer_cancel_wait', 'ack_token_progress'} and not (
                command == 'snapshot' and 'cleanup_subject' in payload):
            return original_send(command_id=command_id, command=command, payload=payload)
        worker.commands.append(command)
        if command == 'bind_request_control':
            # Participant provisioning is a fixture; generation binding is real.
            payload = {key: value for key, value in payload.items() if key != 'token_path'}
        if command == 'infer_start':
            n._token_path_publish = lambda receipt, **kw: worker.progress_inboxes[command_id].feed(receipt)
        if command == 'infer_cancel_wait':
            worker.cancelled.set()
        try:
            result = n.dispatch(dict(protocol='mycelium.physical_node_control.v1', command_id=command_id,
                run_id=n.run_id, deployment_id=n.deployment_id, command=command, payload=payload))
            return _response(n, command_id=command_id, ok=True, result=result)
        except (NodeCommandError, RuntimeError) as error:
            return _response(n, command_id=command_id, ok=False,
                             error_code=getattr(error, 'code', str(error)))
        finally:
            if command == 'infer_start':
                worker.progress_inboxes[command_id].finish('token_progress_missing_terminal')
    worker.send = send
    worker.send_before = send
    worker.native = n
    return cursor


import pytest


@pytest.mark.parametrize('transport', [progress_transport, native_progress_transport])
@pytest.mark.parametrize('late_prefix', [False, True])
def test_real_progress_pump_stop_drains_router_worker(tmp_path, monkeypatch, transport, late_prefix):
    pending_http, release_http = threading.Event(), threading.Event()
    def configured(world, route, worker):
        cursor = transport(world, route, worker)
        send_progress = world['target_client'].token_progress
        def held_progress(**kwargs):
            if kwargs['receipt']['statement']['sequence'] == 1:
                pending_http.set()
                assert release_http.wait(5)
            return send_progress(**kwargs)
        if late_prefix:
            monkeypatch.setattr(world['target_client'], 'token_progress', held_progress)
        return cursor
    with stack(tmp_path, monkeypatch, held_command='streaming',
               progress_factory=configured, restarting_owner=True) as t:
        subscription = t.service.subscribe(t.request_id, last_event_id=None)
        try:
            tokens = []
            first_prefix_tokens = 8 if late_prefix and transport is native_progress_transport else 13
            while len(tokens) < first_prefix_tokens:
                event = subscription.next_event(timeout=3)
                assert event is not None, t.errors
                subscription.ack(event.sequence)
                if event.kind == 'token':
                    tokens.append(event)
                assert event.kind not in {'failed', 'cancelled', 'completed'}
            assert t.worker.progress_sealed
            if late_prefix:
                command_id = next(iter(t.worker.progress_inboxes))
                if transport is progress_transport:
                    journal = t.worker.progress_journals[command_id]
                    # Valid pre-cancel prefix, held only in progress transport.
                    receipt = journal.append(TokenBatch(1, 13, (114,), 'DECODING'))
                    t.worker.progress_inboxes[command_id].feed(receipt)
                assert pending_http.wait(3)
                assert t.router.cancel(t.request_id)
                release_http.set()
            try:
                t.router.close(timeout_seconds=3)
            except RuntimeError:
                print('pump errors:', [(str(e), getattr(e, 'cause_code', None)) for e in t.errors])
                print('operations:', t.worker.commands)
                frames = sys._current_frames()
                for thread in t.router._worker_pool._threads:
                    if thread.ident in frames:
                        traceback.print_stack(frames[thread.ident])
                raise
            assert not any(thread.is_alive() for thread in t.router._worker_pool._threads)
            assert not t.errors
            assert t.worker.cancelled.is_set()
        finally:
            release_http.set()
            subscription.close()
