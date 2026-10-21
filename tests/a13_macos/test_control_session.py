# SPDX-License-Identifier: AGPL-3.0-only
"""Real canonical TLS -> target-owned native node, with durable owner intent."""
from contextlib import contextmanager
import threading
import time
import pytest

from test_control_pump import _world
from test_worker import worker
from test_managed_control import RUN_ID, DEPLOYMENT_ID
from mycelium_a13_macos.control_pump import TargetControlPump
from physical_inference_qualification import ControllerError


@contextmanager
def connected(tmp_path, *, expected_pump_error=None):
    with _world(tmp_path/'swarm') as world:
        root=tmp_path/'native'; root.mkdir()
        w=worker(root,node_id=world['member']['node_id'],run_id=RUN_ID,deployment_id=DEPLOYMENT_ID)
        pump=TargetControlPump(tmp_path/'pump',client=world['target_client'],worker=w,
            current_member=lambda:world['authority'].coordinator.member(world['member']['node_id']))
        stop=threading.Event(); errors=[]
        def run():
            while not stop.is_set():
                try:pump.step()
                except Exception as error:
                    errors.append(str(error));return
                stop.wait(.02)
        thread=threading.Thread(target=run,daemon=True)
        try:
            world['native_initial_hello'] = w.start(); thread.start()
            yield world,w
        finally:
            stop.set();thread.join(timeout=5);pump.close();w.close()
        assert not thread.is_alive()
        assert not w.process_group_present()
        assert not errors or all(error == expected_pump_error for error in errors)


def owner(tmp_path,world,**kwargs):
    from mycelium_a13_macos.control_session import ManagedControlOwner
    return ManagedControlOwner(tmp_path,client=world['owner_client'],
        current_member=lambda node_id:world['authority'].coordinator.member(node_id),**kwargs)


def session(sender, world):
    return sender.session(node_id=world['member']['node_id'],run_id=RUN_ID,
        deployment_id=DEPLOYMENT_ID,timeout_seconds=10)


def test_native_session_send_and_restart_never_reenqueue(tmp_path):
    with connected(tmp_path) as (world,w):
        sender=owner(tmp_path/'sender',world)
        try:
            s=session(sender,world)
            reply=s.send(command_id='canonical-hello',command='hello',payload={})
            assert reply['ok'] is True and reply['result']
            assert reply['route_ready'] is False
            assert w.status()['command_count']==1
            sender.close() # disconnect the owner; do not claim target was stopped
            sender=owner(tmp_path/'sender',world)
            s=session(sender,world)
            assert s.send(command_id='canonical-hello',command='hello',payload={})==reply
            assert w.status()['command_count']==1
            with pytest.raises(ControllerError,match='conflicting_retry'):
                s.send(command_id='canonical-hello',command='health',payload={})
            s.close() # canonical close submits an actual stop command
            assert s.stop_response['ok'] is True
            assert w.status()['command_count']==2
        finally:sender.close()


def test_enqueue_response_lost_recovers_same_native_attempt(tmp_path,monkeypatch):
    with connected(tmp_path) as (world,w):
        sender=owner(tmp_path/'sender',world)
        original=world['owner_client'].enqueue
        def lose(**kwargs):
            original(**kwargs)
            raise TimeoutError('deliberately dropped successful enqueue response')
        try:
            s=session(sender,world)
            monkeypatch.setattr(world['owner_client'],'enqueue',lose)
            with pytest.raises(ControllerError,match='reconciliation_required'):
                s.send(command_id='lost-enqueue',command='hello',payload={})
            sender.close()
            # Restore connectivity; retry is the exact saved signed request,
            # not a second native command. The server idempotency key is unchanged.
            monkeypatch.setattr(world['owner_client'],'enqueue',original)
            world['owner_client']._prepared_requests.clear()
            sender=owner(tmp_path/'sender',world)
            s=session(sender,world)
            reply=s.send(command_id='lost-enqueue',command='hello',payload={})
            assert reply['ok'] is True
            assert w.status()['command_count']==1
        finally:sender.close()


def test_write_failure_precedes_enqueue_and_owner_lock_is_exclusive(tmp_path,monkeypatch):
    with _world(tmp_path/'swarm') as world:
        sender=owner(tmp_path/'sender',world)
        try:
            with pytest.raises(ControllerError,match='owner_already_running'):
                owner(tmp_path/'sender',world)
            calls=[]
            monkeypatch.setattr(world['owner_client'],'enqueue',lambda **kwargs:calls.append(kwargs))
            def fail():raise OSError('deliberate persistence failure')
            monkeypatch.setattr(sender,'_save',fail)
            with pytest.raises(ControllerError,match='state_unavailable'):
                session(sender,world).send(command_id='no-effect',command='hello',payload={})
            assert calls==[]
        finally:sender.close()


def test_membership_change_during_result_fetch_fences_reply(tmp_path,monkeypatch):
    with connected(tmp_path, expected_pump_error='managed_control_member_stale') as (world,w):
        sender=owner(tmp_path/'sender',world)
        original=world['owner_client'].result
        def change_after_reply(**kwargs):
            reply=original(**kwargs)
            if reply['state']=='completed':
                world['authority'].revoke(world['member']['node_id'],world['member']['generation'])
            return reply
        try:
            monkeypatch.setattr(world['owner_client'],'result',change_after_reply)
            with pytest.raises(ControllerError,match='member_stale'):
                session(sender,world).send(command_id='late-revoke',command='hello',payload={})
            assert w.status()['command_count']==1
        finally:sender.close()


def test_canonical_factory_and_hello_verifier_consume_managed_native_reply(tmp_path):
    from physical_inference_qualification import PeerIdentity, QualificationController, _peer_process_argv
    with connected(tmp_path) as (world,w):
        sender=owner(tmp_path/'sender',world)
        try:
            peer=PeerIdentity(node_id=world['member']['node_id'],ssh_target=None,
                host_id=world['native_initial_hello']['result']['host_id'],
                boot_id=world['native_initial_hello']['result']['boot_id'],
                staging_root='/private/tmp/mycelium-managed-fixture',process_transport='managed_https')
            controller=QualificationController(mode='dry-run',peers=(peer,),source_root=tmp_path,
                transfer_manifest={},membership_snapshot={},now=time.time(),session_factory=sender.session_factory)
            s=controller._session_factory(argv=_peer_process_argv(peer,('unused-native-argv',)),
                node_id=peer.node_id,run_id=RUN_ID,deployment_id=DEPLOYMENT_ID,timeout_seconds=10)
            reply=s.send(command_id='canonical-consumer',command='hello',payload={})
            identity=controller._hello_identity(reply,peer=peer,run_id=RUN_ID,deployment_id=DEPLOYMENT_ID)
            assert identity['process_id']==w.pid
            assert identity['state']=='NEW' and identity['route_ready'] is False
            with pytest.raises(ControllerError,match='physical_peer_count_insufficient'):
                controller._validate_physical_distinctness()
            with pytest.raises(ControllerError,match='launch_argv_forbidden'):
                sender.session_factory(argv=('ssh','invalid'),node_id=peer.node_id,
                    run_id=RUN_ID,deployment_id=DEPLOYMENT_ID,timeout_seconds=10)
            s.close()
            assert s.stop_response['ok'] is True
        finally:sender.close()
