# SPDX-License-Identifier: AGPL-3.0-only
"""Crash/response-loss recovery through actual canonical TLS and native hello."""
import pytest
from test_control_session import connected, owner, session
from test_control_pump import _world
from test_managed_control import _enqueue, RUN_ID, DEPLOYMENT_ID
from test_worker import worker
from mycelium_a13_macos.control_pump import TargetControlPump, ControlPumpError
from physical_inference_qualification import ControllerError


def test_owner_recovers_intent_saved_before_any_enqueue_reached_server(tmp_path,monkeypatch):
    with connected(tmp_path) as (world,w):
        sender=owner(tmp_path/'sender',world)
        original=world['owner_client'].enqueue
        def before_network(**kwargs):
            raise TimeoutError('deliberate crash window before HTTP')
        try:
            monkeypatch.setattr(world['owner_client'],'enqueue',before_network)
            with pytest.raises(ControllerError,match='reconciliation_required'):
                session(sender,world).send(command_id='pre-enqueue-gap',command='hello',payload={})
            assert w.status()['command_count']==0
            sender.close()
            monkeypatch.setattr(world['owner_client'],'enqueue',original)
            world['owner_client']._prepared_requests.clear() # a fresh interpreter has no cache
            sender=owner(tmp_path/'sender',world)
            reply=session(sender,world).send(command_id='pre-enqueue-gap',command='hello',payload={})
            assert reply['ok'] is True
            assert w.status()['command_count']==1
        finally:sender.close()


@pytest.mark.parametrize('lost_operation',['poll','ack'])
def test_target_recovers_lost_reply_without_repeating_native_effect(tmp_path,monkeypatch,lost_operation):
    with _world(tmp_path/'swarm') as world:
        root=tmp_path/'native';root.mkdir()
        w=worker(root,node_id=world['member']['node_id'],run_id=RUN_ID,deployment_id=DEPLOYMENT_ID)
        current=lambda:world['authority'].coordinator.member(world['member']['node_id'])
        pump=TargetControlPump(tmp_path/'pump',client=world['target_client'],worker=w,current_member=current)
        original=getattr(world['target_client'],lost_operation)
        sent=[]
        original_request=world['target_client']._request
        def observe(path,envelope):
            if path.endswith('/'+lost_operation):sent.append(envelope)
            return original_request(path,envelope)
        def lose(**kwargs):
            original(**kwargs)
            raise TimeoutError('deliberately dropped committed response')
        try:
            w.start();_enqueue(world,'recover-target')
            monkeypatch.setattr(world['target_client'],'_request',observe)
            monkeypatch.setattr(world['target_client'],lost_operation,lose)
            with pytest.raises((ControlPumpError,TimeoutError)):
                pump.step()
            assert w.status()['command_count']==(0 if lost_operation=='poll' else 1)
            pump.close()
            monkeypatch.setattr(world['target_client'],lost_operation,original)
            world['target_client']._prepared_requests.clear()
            pump=TargetControlPump(tmp_path/'pump',client=world['target_client'],worker=w,current_member=current)
            recovered=pump.step()
            assert recovered['state']=='completed'
            assert recovered['native_response']['ok'] is True
            assert w.status()['command_count']==1
            assert sent[0]==sent[1] # byte-identical signed request replay, including times/nonce
            assert pump.step()['state']=='empty'
            _enqueue(world,'following-command',sequence=2)
            assert pump.step()['state']=='completed'
            assert w.status()['command_count']==2
        finally:pump.close();w.close()
        assert not w.process_group_present()


def test_unknown_native_execution_after_crash_is_not_repeated(tmp_path,monkeypatch):
    with _world(tmp_path/'swarm') as world:
        root=tmp_path/'native';root.mkdir()
        w=worker(root,node_id=world['member']['node_id'],run_id=RUN_ID,deployment_id=DEPLOYMENT_ID)
        current=lambda:world['authority'].coordinator.member(world['member']['node_id'])
        pump=TargetControlPump(tmp_path/'pump',client=world['target_client'],worker=w,current_member=current)
        original=w.send
        def crash_after_native(**kwargs):
            original(**kwargs)
            raise RuntimeError('simulated crash after effect before result persistence')
        try:
            w.start();_enqueue(world,'unknown-native-outcome')
            monkeypatch.setattr(w,'send',crash_after_native)
            with pytest.raises(RuntimeError,match='simulated crash'):pump.step()
            pump.close()
            monkeypatch.setattr(w,'send',original)
            pump=TargetControlPump(tmp_path/'pump',client=world['target_client'],worker=w,current_member=current)
            with pytest.raises(ControlPumpError,match='control_reconciliation_required'):pump.step()
            assert w.status()['command_count']==1
        finally:pump.close();w.close()


def test_tampered_persisted_request_cannot_reach_transport(tmp_path,monkeypatch):
    from mycelium_a13_macos.durable_requests import prepare,replay
    from mycelium_a13_macos.managed_control import ManagedControlError
    from test_managed_control import _command
    with _world(tmp_path/'swarm') as world:
        client=world['owner_client'];calls=[]
        packet=prepare(client,'enqueue',target=world['member'],native_command=_command('honest'),
            sequence=1,nonce='test-nonce',request_id='test-request')
        packet['envelope']['statement']['native_command']['command_id']='tampered'
        monkeypatch.setattr(client,'_request',lambda *args:calls.append(args))
        with pytest.raises(ManagedControlError,match='signature_invalid'):
            replay(client,packet,target=world['member'])
        assert calls==[]
