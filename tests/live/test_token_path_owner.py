# SPDX-License-Identifier: AGPL-3.0-only
"""Owner orchestration units; not a chat/two-process qualification."""
from copy import deepcopy
import importlib
import threading

import pytest

from mycelium_a13_macos.token_receipt_mailbox import MailboxReceiptStream
from mycelium_router.token_path_authority import ReceiptJournal
from mycelium_router.token_path import TokenBatch
from tests.live.test_token_path_admission import world


class Mailbox:
    def __init__(self):
        self.opens = 0
        self.closed = []

    def open_token_progress(self, *, command_id, cursor, seal):
        self.opens += 1
        self.stream = MailboxReceiptStream(cursor, seal, 64)
        return self.stream

    def close_token_progress(self, command_id):
        self.closed.append(command_id)
        self.stream.close()


def owner(w, *, enabled=True, capable=lambda _: True):
    api = importlib.import_module('mycelium_live.token_path_owner')
    box = Mailbox()
    sealed = []
    path = api.OwnerTokenPath(enabled=enabled, signer=w.owner,
        scope_pins=w.pins, entry_key=w.entry.public_key_record(), mailbox=box,
        seal=sealed.append, current=lambda: not w.cancelled, capable=capable,
        max_new_tokens=6, stop_token_ids=(99,),
        process_deadline_monotonic_s=1800., lease_deadline_monotonic_s=100.,
        clock_uncertainty_ms=100, clock=lambda: w.now, unix_ms=lambda:100000)
    return path, box, sealed


def prepare(path, w):
    return path.prepare(command_id='one-start', request=w.request, control=w.control,
        manifest=w.manifest, participating_node_ids={'entry','tail'}, stop_token_ids=(99,))


@pytest.mark.parametrize('enabled,capable', [(False,True),(True,False)])
def test_preflight_fallback_has_no_grant_registration_or_send(world, enabled, capable):
    path, box, sealed = owner(world, enabled=enabled, capable=lambda _:capable)
    assert prepare(path,world) is None
    assert box.opens == 0 and sealed == []


def test_single_start_delivers_verified_deltas_before_final_response(world):
    path, box, sealed = owner(world)
    attempt = prepare(path, world)
    emitted = []
    first_delivered = threading.Event()
    sends = []
    def send(payload):
        sends.append(deepcopy(payload))
        journal = ReceiptJournal(grant=attempt.grant, signer=world.entry, clock=lambda:world.now)
        box.stream.accept(journal.append(TokenBatch(0, 0, (1,), 'DECODING')))
        assert first_delivered.wait(1), 'token must precede the final command response'
        box.stream.accept(journal.append(TokenBatch(1, 1, (2,), 'COMPLETED')))
        return {'result':'native-response'}
    def consume(item):
        emitted.extend(item.token_ids)
        first_delivered.set()
    result = attempt.run(send=send, consume=consume)
    assert result == {'result':'native-response'}
    assert emitted == [1,2] and len(sealed) == 2
    assert len(sends) == 1 and set(sends[0]) == {'request','control','path_manifest','token_grant'}
    assert attempt.token_ids == (1,2) and attempt.status == 'COMPLETED'
    assert box.closed == ['one-start']
    with pytest.raises(RuntimeError, match='token_path_grant_replay'):
        attempt.run(send=send,consume=consume)
    assert len(sends) == 1


def test_command_success_without_terminal_receipt_is_never_completion(world):
    path, box, _ = owner(world)
    attempt = prepare(path, world)
    with pytest.raises(RuntimeError, match='token_progress_terminal_missing'):
        attempt.run(send=lambda _: {'ok':True}, consume=lambda _:None)
    assert box.closed == ['one-start']


def test_cancel_before_dispatch_sends_no_start(world):
    path, box, _ = owner(world)
    attempt=prepare(path,world)
    calls=[]
    with pytest.raises(RuntimeError,match='request_cancelled'):
        attempt.run(send=lambda _:calls.append(True),consume=lambda _:None,cancelled=lambda:True)
    assert not calls


def test_cancel_interrupts_held_start_without_waiting_for_compute(world):
    path, box, _ = owner(world)
    attempt = prepare(path, world)
    entered, release = threading.Event(), threading.Event()
    interrupted=[]
    def send(_):
        entered.set()
        assert release.wait(2)
        return {'ok':True}
    def interrupt():
        interrupted.append(True)
        release.set()
    try:
        with pytest.raises(RuntimeError,match='request_cancelled'):
            attempt.run(send=send,consume=lambda _:pytest.fail('cancelled tokens'),
                interrupt=interrupt,cancelled=entered.is_set)
        assert interrupted==[True] and box.closed==['one-start']
    finally:
        release.set()


def test_failed_start_never_retries_or_falls_back(world):
    path, box, _ = owner(world)
    attempt = prepare(path, world)
    sends=[]
    def send(payload):
        sends.append(payload)
        raise RuntimeError('native_start_refused')
    with pytest.raises(RuntimeError,match='native_start_refused'):
        attempt.run(send=send,consume=lambda _:None)
    assert len(sends)==1 and box.closed==['one-start']


@pytest.mark.parametrize('change',['participants','stops','manifest','owner','entry'])
def test_wrong_route_or_key_cannot_issue_a_grant(world, change):
    if change=='owner':
        world.pins['owner_key_digest']='sha256:'+'f'*64
    elif change=='entry':
        world.pins['entry_key_digest']='sha256:'+'f'*64
    path,box,_=owner(world)
    if change=='participants': world.pins['participants'].pop()  # caller mutation cannot change pins
    args=dict(command_id='start',request=world.request,control=world.control,
        manifest=world.manifest,participating_node_ids={'entry','tail'},stop_token_ids=(99,))
    if change=='participants': args['participating_node_ids']={'entry'}
    elif change=='stops': args['stop_token_ids']=(98,)
    elif change=='manifest': args['manifest']={'path_id':'other'}
    with pytest.raises((ValueError,RuntimeError)):
        path.prepare(**args)
    assert box.opens==0


@pytest.mark.parametrize('outcome', ['refused', 'missing-terminal', 'completed', 'revoked'])
def test_timeout_then_command_completion_drains_verified_prefix(world, monkeypatch, outcome):
    """Pin the empty-read/completed-command race with real signed receipts."""
    from concurrent.futures import Future
    api = importlib.import_module('mycelium_live.token_path_owner')
    empty_read, command_done = threading.Event(), threading.Event()

    class ObservedFuture(Future):
        def set_result(self, result):
            super().set_result(result)
            command_done.set()

        def set_exception(self, exception):
            super().set_exception(exception)
            command_done.set()

    monkeypatch.setattr(api, 'Future', ObservedFuture)
    path, box, sealed = owner(world)
    attempt = prepare(path, world)
    read = box.stream.read
    first = True

    def held_timeout(*, timeout):
        nonlocal first
        if not first:
            return read(timeout=timeout)
        first = False
        try:
            return read(timeout=0)
        except TimeoutError:
            # Empty read has released its condition lock. The real producer
            # now seals/enqueues its prefix and completes the command before
            # the consumer observes that TimeoutError.
            empty_read.set()
            assert command_done.wait(2), 'command must finish in the timeout gap'
            raise

    monkeypatch.setattr(box.stream, 'read', held_timeout)
    sends, emitted = [], []

    def send(payload):
        sends.append(payload)
        assert empty_read.wait(2), 'read must observe the initial empty queue'
        journal = ReceiptJournal(grant=attempt.grant, signer=world.entry, clock=lambda: world.now)
        box.stream.accept(journal.append(TokenBatch(0, 0, (1,), 'DECODING')))
        status = 'COMPLETED' if outcome == 'completed' else 'DECODING'
        box.stream.accept(journal.append(TokenBatch(1, 1, (2,), status)))
        if outcome == 'revoked':
            world.cancelled = True
        if outcome in ('refused', 'revoked'):
            raise RuntimeError('native_start_refused')
        return {'result': 'native-response'}

    if outcome == 'completed':
        assert attempt.run(send=send, consume=lambda item: emitted.extend(item.token_ids)) == {
            'result': 'native-response'}
    else:
        code = {'refused': 'native_start_refused', 'missing-terminal': 'token_progress_terminal_missing',
                'revoked': 'token_path_grant_stale'}[outcome]
        from mycelium_router.token_path_authority import TokenPathAuthorityError
        error_type = TokenPathAuthorityError if outcome == 'revoked' else RuntimeError
        with pytest.raises(error_type, match=code):
            attempt.run(send=send, consume=lambda item: emitted.extend(item.token_ids))
    expected = [] if outcome == 'revoked' else [1, 2]
    assert emitted == expected and attempt.token_ids == tuple(expected)
    assert len(sends) == 1 and len(sealed) == 2 and box.closed == ['one-start']
