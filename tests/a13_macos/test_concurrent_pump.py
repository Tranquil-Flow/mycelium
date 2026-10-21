# SPDX-License-Identifier: AGPL-3.0-only
"""Real TLS/native lifecycle with a blocked dispatch callback; no model work."""
import threading
import time
import pytest
from test_control_pump import _world
from test_managed_control import _enqueue,RUN_ID,DEPLOYMENT_ID
from test_worker import worker
from mycelium_a13_macos.control_pump import ControlPumpError


def make_pump(root,world,w):
    from mycelium_a13_macos.concurrent_pump import ConcurrentTargetControlPump
    return ConcurrentTargetControlPump(root,client=world['target_client'],worker=w,
        current_member=lambda:world['authority'].coordinator.member(world['member']['node_id']))


def until_ack(pump,command_id):
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        result=pump.step()
        if result.get('command_id')==command_id and result['state']=='completed':return result
        time.sleep(.01)
    pytest.fail('native result was not acknowledged')


def test_polling_and_control_continue_while_one_dispatch_is_blocked(tmp_path,monkeypatch):
    with _world(tmp_path/'swarm') as world:
        root=tmp_path/'native';root.mkdir()
        w=worker(root,node_id=world['member']['node_id'],run_id=RUN_ID,deployment_id=DEPLOYMENT_ID)
        entered=threading.Event();release=threading.Event();original=w.send
        def dispatch(**kwargs):
            if kwargs['command_id']=='blocked-dispatch':entered.set();release.wait(5)
            return original(**kwargs)
        monkeypatch.setattr(w,'send',dispatch)
        pump=make_pump(tmp_path/'pump',world,w)
        try:
            w.start();_enqueue(world,'blocked-dispatch')
            assert pump.step()['state']=='dispatched'
            assert entered.wait(1)
            _enqueue(world,'independent-control',sequence=2)
            second=until_ack(pump,'independent-control')
            assert second['native_response']['ok'] is True
            assert second['native_response']['result']['process_id']==w.pid
            assert w.status()['command_count']==1
            release.set()
            assert until_ack(pump,'blocked-dispatch')['native_response']['ok'] is True
            assert w.status()['command_count']==2
        finally:release.set();pump.close();w.close()
        assert not w.process_group_present()


def test_concurrent_poll_response_replay_never_loses_claim(tmp_path,monkeypatch):
    with _world(tmp_path/'swarm') as world:
        root=tmp_path/'native';root.mkdir()
        w=worker(root,node_id=world['member']['node_id'],run_id=RUN_ID,deployment_id=DEPLOYMENT_ID)
        pump=make_pump(tmp_path/'pump',world,w)
        original=world['target_client'].poll
        def lose(**kwargs):original(**kwargs);raise TimeoutError('lost committed poll')
        try:
            w.start();_enqueue(world,'recovered-poll')
            monkeypatch.setattr(world['target_client'],'poll',lose)
            with pytest.raises(ControlPumpError):pump.step()
            monkeypatch.setattr(world['target_client'],'poll',original)
            assert until_ack(pump,'recovered-poll')['native_response']['ok'] is True
            assert w.status()['command_count']==1
        finally:pump.close();w.close()


def test_owner_reconciliation_demand_is_answered_not_fatal(tmp_path,monkeypatch):
    """One lost committed poll reply must not brick the peer permanently.

    The owner keeps the ``claimed`` row; the peer's fresh poll cannot list that
    command in ``active_claims``; the owner answers with
    ``delivery_kind='reconciliation_required'``.  Dying on that reply (the previous
    behaviour) meant the peer never acknowledged, so the owner's row never reached a
    terminal state, every later poll re-demanded the same command, and the peer
    stopped claiming work for the rest of the run (round45: 5 claimed of 244).
    Answering the demand never re-runs native work: an unknown outcome is still
    never re-executed.
    """
    import mycelium_a13_macos.control_pump as pump_module
    from mycelium_a13_macos.managed_control import ManagedControlError
    with _world(tmp_path/'swarm') as world:
        root=tmp_path/'native';root.mkdir()
        w=worker(root,node_id=world['member']['node_id'],run_id=RUN_ID,deployment_id=DEPLOYMENT_ID)
        pump=make_pump(tmp_path/'pump',world,w)
        real=pump_module.replay
        state={'expired':False}
        def expire_after_commit(client,prepared,*,target):
            result=real(client,prepared,target=target)
            if prepared['operation']=='poll' and not state['expired']:
                state['expired']=True
                # The owner committed this claim; the peer drops the dead envelope
                # without ever recording the claim.
                raise ManagedControlError('managed_control_message_expired')
            return result
        try:
            w.start();_enqueue(world,'lost-claim')
            monkeypatch.setattr(pump_module,'replay',expire_after_commit)
            assert pump.step()=={'state':'outbound_expired_requeued','operation':'poll'}
            assert pump._claims_state['claims']=={}
            # A fresh poll cannot list the owner-claimed command, so the owner
            # demands reconciliation instead of re-delivering it.
            monkeypatch.setattr(pump_module,'replay',real)
            answered=pump.step()
            assert answered['state']=='reconciliation_required'
            assert answered['command_id']=='lost-claim'
            assert answered['native_response'] is None
            # The owner's row is now terminal, so later polls stop demanding it.
            # `world['owner_client']`/`target_client` here address the authority's own
            # seed listener, so its mailbox is the one that holds the commands.
            assert world['authority'].control_mailbox.command_receipt('lost-claim')['state']=='reconciliation_required'
            assert pump.step()=={'state':'empty'}
            # Real work continues on the same control loop.
            _enqueue(world,'following-command',sequence=2)
            assert until_ack(pump,'following-command')['native_response']['ok'] is True
            assert w.status()['command_count']==1
        finally:pump.close();w.close()


def test_reconciliation_demand_with_an_in_flight_dispatch_still_fails_closed(tmp_path,monkeypatch):
    """A demand for a claim whose native dispatch is still in flight stays fatal.

    An outcome may still arrive in this process, so the uncertainty is real and
    must not be summarised as "unknown" before the dispatch settles.
    """
    import concurrent.futures
    import mycelium_a13_macos.control_pump as pump_module
    with _world(tmp_path/'swarm') as world:
        root=tmp_path/'native';root.mkdir()
        w=worker(root,node_id=world['member']['node_id'],run_id=RUN_ID,deployment_id=DEPLOYMENT_ID)
        pump=make_pump(tmp_path/'pump',world,w)
        def demand(client,prepared,*,target):
            if prepared['operation']=='poll':
                return {'delivery_kind':'reconciliation_required','command_id':'in-flight',
                        'claim_token':'claim-token-in-flight','command_envelope':None}
            raise AssertionError('no ack may be prepared for an in-flight dispatch')
        try:
            w.start()
            pump._claims_state['claims']['in-flight']={'phase':'executing',
                'reply':{'command_id':'in-flight','claim_token':'claim-token-in-flight'},
                'native_response':None,'error_code':None}
            pump._futures['in-flight']=concurrent.futures.Future()
            pump._save_claims()
            monkeypatch.setattr(pump_module,'replay',demand)
            with pytest.raises(ControlPumpError) as info:pump.step()
            assert str(info.value)=='control_reconciliation_required'
        finally:pump.close();w.close()


def test_concurrent_state_cannot_be_reopened_by_serial_pump(tmp_path):
    from mycelium_a13_macos.control_pump import TargetControlPump
    with _world(tmp_path/'swarm') as world:
        root=tmp_path/'native';root.mkdir()
        w=worker(root,node_id=world['member']['node_id'],run_id=RUN_ID,deployment_id=DEPLOYMENT_ID)
        pump=make_pump(tmp_path/'pump',world,w);pump.close()
        with pytest.raises(ControlPumpError,match='concurrent_state_requires_concurrent_pump'):
            TargetControlPump(tmp_path/'pump',client=world['target_client'],worker=w,
                current_member=lambda:world['member'])
        w.close()


def test_a_held_poll_step_claims_a_command_enqueued_mid_hold(tmp_path):
    """The delivered step, not just the mailbox API.

    The peer's control loop is parked inside `step()` for up to the hold now, so
    the end-to-end claim has to happen while that very call is in flight, and the
    pump must return the delivery from the same call.  This is the shape the
    physical two-Mac run depends on: a cleanup command enqueued after the poll
    departed must be claimed inside the pump step that is already waiting, instead
    of a full relay cycle later.
    """
    from test_managed_control import _world as _mailbox_world
    with _mailbox_world(tmp_path/'swarm',mailbox_options={'long_poll_hold':1.5}) as world:
        root=tmp_path/'native';root.mkdir()
        w=worker(root,node_id=world['member']['node_id'],run_id=RUN_ID,deployment_id=DEPLOYMENT_ID)
        pump=make_pump(tmp_path/'pump',world,w)
        outcome={}
        try:
            w.start()
            def step():
                try:outcome['result']=pump.step()
                except BaseException as error:outcome['error']=error
            parked=threading.Thread(target=step,name='held-pump-step',daemon=True)
            parked.start()
            parked.join(.4)
            assert parked.is_alive(),'the pump step returned before a command existed'
            started=time.monotonic()
            _enqueue(world,'held-pump-command')
            parked.join(10)
            assert not parked.is_alive(),'the held pump step never returned'
            if 'error' in outcome:raise outcome['error']
            result=outcome['result']
            assert result['state']=='dispatched' and result['command_id']=='held-pump-command'
            assert time.monotonic()-started < .5,'the held step claimed a full relay cycle late'
        finally:pump.close();w.close()
