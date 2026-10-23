# SPDX-License-Identifier: AGPL-3.0-only
"""CPU-only native pipe progress: signatures are real, compute is fake.

No managed mailbox, device, model or cleanup authority is simulated as proof.
"""
from copy import deepcopy
import importlib
import json
from pathlib import Path
import sys
import threading
import time


import pytest

from mycelium_node.process import PhysicalNodeProcess
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_router.token_path import TokenBatch
from mycelium_router.token_path_authority import ReceiptCursor, ReceiptJournal
from tests.live.test_token_path_authority import setup, admit, issue


def api():
    return importlib.import_module('mycelium_node.token_progress')


def components(setup, **kwargs):
    now = time.monotonic()
    grant = admit(setup, now_monotonic_s=now,
                  process_deadline_monotonic_s=now+1800,
                  lease_deadline_monotonic_s=now+100)
    cursor = ReceiptCursor(grant=grant, entry_key=setup[1].public_key_record())
    journal = ReceiptJournal(grant=grant, signer=setup[1])
    inbox = api().ReceiptInbox(cursor=cursor, deadline_monotonic_s=time.monotonic() + 3,
                               **kwargs)
    return journal, cursor, inbox


def test_managed_worker_registers_pinned_progress_on_shared_session(tmp_path, setup):
    from tests.a13_macos.test_worker import worker
    w = worker(tmp_path)
    journal, cursor, inbox = components(setup)
    calls = []
    class Session:
        def open_progress(self, **kwargs):
            calls.append(kwargs)
            return inbox
        def close_progress(self, command_id, *, code):
            calls.append((command_id, code))
            inbox.close(code)
    w._session = Session()
    w._check = lambda *args: None
    deadline = time.monotonic() + 1
    assert w.open_progress(command_id='progress-start', cursor=cursor,
                           deadline_monotonic_s=deadline) is inbox
    assert calls[0]['cursor'] is cursor and calls[0]['deadline_monotonic_s'] == deadline
    w.close_progress('progress-start', code='request_cancelled')
    with pytest.raises(RuntimeError, match='request_cancelled'):
        inbox.read()


def test_verify_before_return_and_no_implicit_ack(setup):
    journal, cursor, inbox = components(setup)
    receipt = journal.append(TokenBatch(0, 0, (1, 2), 'DECODING'))
    inbox.feed(receipt)
    receipt['statement']['token_ids'][0] = 100
    verified = inbox.read()
    assert verified.token_ids == (1, 2)
    assert verified.sequence == 0
    assert verified.receipt_digest == journal.read(after_sequence=-1)[0]['signature']['signed_statement_digest']
    assert cursor.terminal is None
    assert len(journal.read(after_sequence=-1)) == 1


def test_tampered_signature_poison_is_request_local(setup):
    journal, cursor, inbox = components(setup)
    receipt = journal.append(TokenBatch(0, 0, (1,), 'DECODING'))
    receipt['statement']['token_ids'] = [2]
    inbox.feed(receipt)
    with pytest.raises(ValueError, match='token_path_receipt_signature'):
        inbox.read()
    with pytest.raises(RuntimeError, match='token_progress_invalid_receipt'):
        inbox.read()
    assert cursor.terminal is None


def test_overflow_never_blocks_reader_or_conceals_lost_prefix(setup):
    journal, _, inbox = components(setup, capacity=1)
    inbox.feed(journal.append(TokenBatch(0, 0, (1,), 'DECODING')))
    inbox.feed(journal.append(TokenBatch(1, 1, (2,), 'DECODING')))
    with pytest.raises(RuntimeError, match='token_progress_overflow'):
        inbox.read()


def test_cancel_interrupts_empty_wait_without_accepting_late_tokens(setup):
    journal, cursor, inbox = components(setup)
    result = []
    started = threading.Event()
    def wait():
        started.set()
        try:
            inbox.read()
        except RuntimeError as exc:
            result.append(str(exc))
    thread = threading.Thread(target=wait)
    thread.start()
    assert started.wait(1)
    inbox.close('request_cancelled')
    thread.join(1)
    assert not thread.is_alive()
    assert result == ['request_cancelled']
    inbox.feed(journal.append(TokenBatch(0, 0, (1,), 'COMPLETED')))
    assert cursor.terminal is None


def test_deadline_is_absolute_and_expired_read_does_not_publish(setup):
    journal, cursor, _ = components(setup)
    inbox = api().ReceiptInbox(cursor=cursor, deadline_monotonic_s=time.monotonic() - 1)
    inbox.feed(journal.append(TokenBatch(0, 0, (1,), 'COMPLETED')))
    with pytest.raises(TimeoutError, match='command_deadline_exceeded'):
        inbox.read()
    assert cursor.terminal is None


def test_eof_is_not_empty_success_but_drain_terminal_before_eof(setup):
    journal, cursor, inbox = components(setup)
    inbox.feed(journal.append(TokenBatch(0, 0, (1,), 'COMPLETED')))
    inbox.finish('node_process_exited')
    assert inbox.read().status == 'COMPLETED'
    assert cursor.terminal == 'COMPLETED'
    with pytest.raises(RuntimeError, match='token_progress_terminal'):
        inbox.read()
    _, _, empty = components(setup)
    empty.finish('node_process_exited')
    with pytest.raises(RuntimeError, match='node_process_exited'):
        empty.read()


def test_frame_exact_binding_and_private_size_limit(setup):
    journal, _, _ = components(setup)
    receipt = journal.append(TokenBatch(0, 0, (1,), 'COMPLETED'))
    frame = api().progress_frame(command_id='start-1', node_id='entry', receipt=receipt)
    assert api().validate_progress_frame(frame, node_id='entry') == frame
    for key, value in [('node_id', 'other'), ('unknown', 1), ('command_id', True)]:
        changed = deepcopy(frame)
        changed[key] = value
        with pytest.raises(ValueError, match='token_progress_frame_invalid'):
            api().validate_progress_frame(changed, node_id='entry')
    with pytest.raises(ValueError, match='token_progress_frame_invalid'):
        api().progress_frame(command_id='start-1', node_id='entry', receipt={'padding': 'x' * 20000})


def test_real_pipe_receipts_arrive_before_command_result_and_cleanup(setup, tmp_path):
    # A private CPU child drives the shipped native adapter over DeskNode's fake
    # two-stage primitive, not precomputed output. Only these ephemeral test keys
    # cross a scratch file; no fleet membership or production signer is loaded.
    from cryptography.hazmat.primitives import serialization
    _, cursor, _ = components(setup)
    fixture = tmp_path / 'authority.json'
    fixture.write_text(json.dumps(dict(
        scope=setup[2], owner_key=setup[0].public_key_record(), grant=issue(setup),
        entry_secret=setup[1]._private_key.private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
            serialization.NoEncryption()).hex())))
    fixture.chmod(0o600)
    child = tmp_path / 'child.py'
    child.write_text('''import json, sys, threading, time
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from mycelium_qualification.signing import Ed25519EvidenceSigner
from mycelium_router.token_path_authority import GrantRegistry, ReceiptJournal
from tests.live.test_entry_token_path import DeskNode
from physical_inference_node import _ResponseEmitter
emitter = _ResponseEmitter()
with open(sys.argv[1]) as source:
    authority = json.load(source)
secret = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(authority['entry_secret']))
signer = Ed25519EvidenceSigner(endpoint_id=authority['scope']['entry_endpoint_id'],
    _private_key=secret, _public_key_bytes=secret.public_key().public_bytes_raw())
now = time.monotonic()
grant = GrantRegistry().admit(authority['grant'], expected_scope=authority['scope'],
    owner_key=authority['owner_key'], max_new_tokens=6, stop_token_ids=(99,),
    now_unix_ms=100000, now_monotonic_s=now, process_deadline_monotonic_s=now+1800,
    lease_deadline_monotonic_s=now+100, clock_uncertainty_ms=100)
journal = ReceiptJournal(grant=grant, signer=signer)
node = DeskNode()
node.node.signer = signer
release = threading.Event()
def execute(command):
    cid = command['command_id']
    if command['command'] == 'infer_start':
        node.node.drive_granted_entry_token_path(grant=grant, journal=journal,
            publish=lambda receipt: emitter.emit_progress(command_id=cid, node_id='entry',
                receipt=receipt, deadline_monotonic_s=grant.deadline_monotonic_s),
            cancelled=lambda: False, enabled=True, batch_tokens=2)
        release.wait(3)
    else:
        release.set()
    emitter.emit(dict(protocol='mycelium.physical_node_control.v1', command_id=cid,
                      node_id='entry', ok=True, route_ready=False, result={'rounds': node.rounds}),
                 cleanup_priority=command['command']=='infer_cancel_wait')
for raw in sys.stdin:
    command=json.loads(raw)
    if command['command']=='stop':
        execute(command)
        break
    threading.Thread(target=execute, args=(command,)).start()
''')
    # Explicit source path for this private child; not an inherited PYTHONPATH.
    bootstrap = "import sys,runpy;sys.path.insert(0,sys.argv.pop(1));p=sys.argv.pop(1);sys.argv[0]=p;runpy.run_path(p,run_name='__main__')"
    root = str(Path(__file__).resolve().parents[2])
    process = PhysicalNodeProcess(command=(sys.executable, '-c', bootstrap, root, str(child), str(fixture)),
        node_id='entry', run_id='desk', deployment_id='deployment', response_timeout_seconds=4)
    result, errors = [], []
    thread = None
    try:
        inbox = process.open_progress(command_id='start-1', cursor=cursor,
                                      deadline_monotonic_s=time.monotonic()+3)
        def start():
            try:
                result.append(process.command('infer_start', {}, command_id='start-1'))
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=start)
        thread.start()
        assert inbox.read().token_ids == (1, 2)
        assert inbox.read().token_ids == (3, 4)
        assert inbox.read().token_ids == (5, 6)
        assert result == [] and thread.is_alive()
        assert process.command('infer_cancel_wait', {}, command_id='cancel-1') == {'rounds': 6}
        thread.join(2)
        assert not thread.is_alive() and not errors
        assert result == [{'rounds': 6}]
        assert process.stderr_tail == ''
        process.close_progress('start-1')
    finally:
        process.close()
        if thread is not None:
            thread.join(2)


def test_progress_cannot_fill_ordinary_response_slot(setup):
    from queue import Queue
    from collections import OrderedDict
    journal, cursor, _ = components(setup)
    process = object.__new__(PhysicalNodeProcess)
    process.node_id = 'entry'
    process.run_id = 'desk'
    process.deployment_id = 'deployment'
    process._waiters_lock = threading.RLock()
    process._waiters = {'start-1': Queue(maxsize=1), 'cleanup': Queue(maxsize=1)}
    process._retired_command_ids = OrderedDict()
    process._reader_failure_code = None
    inbox = process.open_progress(command_id='start-1', cursor=cursor,
                                  deadline_monotonic_s=time.monotonic()+3, capacity=1)
    for index in range(2):
        receipt = journal.append(TokenBatch(index, index, (index+1,), 'DECODING'))
        frame = api().progress_frame(command_id='start-1', node_id='entry', receipt=receipt)
        assert process._deliver_response(canonical_json_bytes(frame))
    assert process._waiters['start-1'].empty()
    assert process._waiters['cleanup'].empty()
    assert process._reader_failure_code is None
    with pytest.raises(RuntimeError, match='token_progress_overflow'):
        inbox.read()
    process.close_progress('start-1')
    assert process._deliver_response(canonical_json_bytes(frame))
    with pytest.raises(RuntimeError, match='token_progress_replay'):
        process.open_progress(command_id='start-1', cursor=cursor,
                              deadline_monotonic_s=time.monotonic()+3)


def test_native_worker_facade_preserves_verified_stream_and_interrupt(setup):
    from mycelium_a13_macos.worker import _ConcurrentNativeSession
    journal, cursor, _ = components(setup)
    session = object.__new__(_ConcurrentNativeSession)
    session._process = bare_process()
    inbox = session.open_progress(command_id='start', cursor=cursor,
                                  deadline_monotonic_s=time.monotonic()+3)
    frame = api().progress_frame(command_id='start', node_id='entry',
        receipt=journal.append(TokenBatch(0, 0, (1,), 'DECODING')))
    assert session._process._deliver_response(canonical_json_bytes(frame))
    assert inbox.read().token_ids == (1,)
    assert session.interrupt_command('start', code='request_cancelled')
    with pytest.raises(RuntimeError, match='request_cancelled'):
        inbox.read()
    session.close_progress('start')


def test_progress_must_not_use_independent_cleanup_lane(setup):
    import io
    from types import SimpleNamespace
    from mycelium_node.process import CLEANUP_CONTROL_FRAME_PREFIX
    journal, cursor, _ = components(setup)
    process = bare_process()
    inbox = process.open_progress(command_id='start', cursor=cursor,
                                  deadline_monotonic_s=time.monotonic()+3)
    frame = api().progress_frame(command_id='start', node_id='entry',
        receipt=journal.append(TokenBatch(0, 0, (1,), 'COMPLETED')))
    process._process = SimpleNamespace(stderr=io.BytesIO(
        CLEANUP_CONTROL_FRAME_PREFIX + canonical_json_bytes(frame) + b'\n'))
    process.max_frame_bytes = 1024 * 1024
    process._read_stderr()
    assert process._reader_failure_code == 'token_progress_wrong_lane'
    with pytest.raises(RuntimeError, match='token_progress_wrong_lane'):
        inbox.read()
    assert cursor.terminal is None


def bare_process():
    from collections import OrderedDict
    process = object.__new__(PhysicalNodeProcess)
    process.node_id, process.run_id, process.deployment_id = 'entry', 'desk', 'deployment'
    process._waiters_lock = threading.RLock()
    process._waiters = {}
    process._retired_command_ids = OrderedDict()
    process._reader_failure_code = None
    return process


@pytest.mark.parametrize('field', ['node_id', 'run_id', 'deployment_id'])
def test_registered_stream_must_match_native_process_identity(setup, field):
    _, cursor, _ = components(setup)
    process = bare_process()
    setattr(process, field, 'different')
    with pytest.raises(RuntimeError, match='token_progress_binding_mismatch'):
        process.open_progress(command_id='start', cursor=cursor,
                              deadline_monotonic_s=time.monotonic()+3)


def test_stream_registry_caps_active_and_spent_ids_without_eviction(setup):
    _, cursor, _ = components(setup)
    process = bare_process()
    for index in range(8):
        process.open_progress(command_id=f'start-{index}', cursor=cursor,
                              deadline_monotonic_s=time.monotonic()+3)
    with pytest.raises(RuntimeError, match='token_progress_capacity'):
        process.open_progress(command_id='ninth', cursor=cursor,
                              deadline_monotonic_s=time.monotonic()+3)
    for index in range(8):
        process.close_progress(f'start-{index}')
    for index in range(8, 4096):
        process.open_progress(command_id=f'start-{index}', cursor=cursor,
                              deadline_monotonic_s=time.monotonic()+3)
        process.close_progress(f'start-{index}')
    with pytest.raises(RuntimeError, match='token_progress_capacity'):
        process.open_progress(command_id='new', cursor=cursor,
                              deadline_monotonic_s=time.monotonic()+3)
    with pytest.raises(RuntimeError, match='token_progress_replay'):
        process.open_progress(command_id='start-0', cursor=cursor,
                              deadline_monotonic_s=time.monotonic()+3)


def test_interrupt_before_command_write_closes_registered_receipts(setup):
    journal, cursor, _ = components(setup)
    process = bare_process()
    inbox = process.open_progress(command_id='start', cursor=cursor,
                                  deadline_monotonic_s=time.monotonic()+3)
    assert process.interrupt_command('start', code='request_cancelled')
    frame = api().progress_frame(command_id='start', node_id='entry',
        receipt=journal.append(TokenBatch(0, 0, (1,), 'COMPLETED')))
    assert process._deliver_response(canonical_json_bytes(frame))
    with pytest.raises(RuntimeError, match='request_cancelled'):
        inbox.read()
    assert cursor.terminal is None


def test_cancel_overtakes_blocked_verification_without_blocking_reader(setup):
    journal, cursor, inbox = components(setup)
    entered, release = threading.Event(), threading.Event()
    def current():
        entered.set()
        assert release.wait(2)
        return True
    cursor._current = current
    inbox.feed(journal.append(TokenBatch(0, 0, (1,), 'DECODING')))
    errors = []
    def consume():
        try:
            inbox.read()
        except RuntimeError as exc:
            errors.append(str(exc))
    thread = threading.Thread(target=consume)
    thread.start()
    try:
        assert entered.wait(1)
        # Feed and cancel must not wait behind the consumer's verification.
        inbox.feed(journal.append(TokenBatch(1, 1, (2,), 'COMPLETED')))
        inbox.close('request_cancelled')
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()
    assert errors == ['request_cancelled']


def test_gap_and_duplicate_receipts_fail_before_token_delivery(setup):
    journal, _, inbox = components(setup)
    first = journal.append(TokenBatch(0, 0, (1,), 'DECODING'))
    second = journal.append(TokenBatch(1, 1, (2,), 'COMPLETED'))
    inbox.feed(second)
    with pytest.raises(ValueError, match='token_path_receipt_order'):
        inbox.read()
    _, _, inbox = components(setup)
    inbox.feed(first)
    assert inbox.read().token_ids == (1,)
    inbox.feed(first)
    with pytest.raises(ValueError, match='token_path_receipt_order'):
        inbox.read()


def test_write_wait_deadline_and_cancel_leave_cleanup_independent(setup, monkeypatch):
    import physical_inference_node as native
    journal, _, _ = components(setup)
    receipt = journal.append(TokenBatch(0, 0, (1,), 'COMPLETED'))
    emitter = native._ResponseEmitter()
    emitter._stdout_active = True
    writes = []
    monkeypatch.setattr(native, '_emit', lambda data, **kw: writes.append((data, kw)))
    args = dict(command_id='start', node_id='entry', receipt=receipt)
    with pytest.raises(RuntimeError, match='command_deadline_exceeded'):
        emitter.emit_progress(**args, deadline_monotonic_s=time.monotonic()+0.02)
    with pytest.raises(RuntimeError, match='request_cancelled'):
        emitter.emit_progress(**args, deadline_monotonic_s=time.monotonic()+3,
                              cancelled=lambda: True)
    emitter.emit({'cleanup': True}, cleanup_priority=True)
    assert len(writes) == 1 and writes[0][1]['cleanup_priority'] is True


def test_full_pipe_deadline_is_bounded_and_blocking_mode_restored(setup, monkeypatch):
    import os
    from types import SimpleNamespace
    import physical_inference_node as native
    journal, _, _ = components(setup)
    receipt = journal.append(TokenBatch(0, 0, (1,), 'COMPLETED'))
    emitter = native._ResponseEmitter()
    read_fd, write_fd = os.pipe()
    try:
        os.set_blocking(write_fd, False)
        while True:
            try:
                os.write(write_fd, b'x' * 4096)
            except BlockingIOError:
                break
        os.set_blocking(write_fd, True)
        with monkeypatch.context() as context:
            context.setattr(native.sys, 'stdout', SimpleNamespace(buffer=SimpleNamespace(fileno=lambda: write_fd)))
            with pytest.raises(RuntimeError, match='command_deadline_exceeded'):
                emitter.emit_progress(command_id='start', node_id='entry', receipt=receipt,
                                      deadline_monotonic_s=time.monotonic()+0.03)
        assert os.get_blocking(write_fd) is True
        assert emitter._stdout_active is False
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_partial_write_poison_never_concatenates_json_but_cleanup_survives(setup, monkeypatch):
    import os
    from types import SimpleNamespace
    import physical_inference_node as native
    journal, _, _ = components(setup)
    receipt = journal.append(TokenBatch(0, 0, (1,), 'COMPLETED'))
    emitter = native._ResponseEmitter()
    read_fd, write_fd = os.pipe()
    real_write = os.write
    calls = []
    def partial(fd, data):
        calls.append(fd)
        return real_write(fd, data[:1])
    try:
        with monkeypatch.context() as context:
            context.setattr(native.sys, 'stdout', SimpleNamespace(buffer=SimpleNamespace(fileno=lambda: write_fd)))
            context.setattr(os, 'write', partial)
            with pytest.raises(RuntimeError, match='request_cancelled'):
                emitter.emit_progress(command_id='start', node_id='entry', receipt=receipt,
                    deadline_monotonic_s=time.monotonic()+3, cancelled=lambda: bool(calls))
        assert emitter._stdout_failed
        with pytest.raises(RuntimeError, match='token_progress_pipe_failed'):
            emitter.emit({'ordinary': True})
        writes = []
        monkeypatch.setattr(native, '_emit', lambda data, **kw: writes.append((data, kw)))
        emitter.emit({'cleanup': True}, cleanup_priority=True)
        assert len(writes) == 1 and writes[0][1]['cleanup_priority'] is True
    finally:
        os.close(write_fd)
        os.close(read_fd)
