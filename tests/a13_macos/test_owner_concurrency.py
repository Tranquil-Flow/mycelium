# SPDX-License-Identifier: AGPL-3.0-only
"""Concurrent owner waiters over real TLS/native hello; not inference proof."""
from contextlib import contextmanager
import threading
import time
import pytest
from test_control_pump import _world
from test_control_session import owner,session
from test_worker import worker
from test_managed_control import RUN_ID,DEPLOYMENT_ID
from mycelium_a13_macos.concurrent_pump import ConcurrentTargetControlPump
from physical_inference_qualification import ControllerError


@contextmanager
def connected(tmp_path):
    with _world(tmp_path/'swarm') as world:
        root=tmp_path/'native';root.mkdir()
        w=worker(root,node_id=world['member']['node_id'],run_id=RUN_ID,deployment_id=DEPLOYMENT_ID)
        pump=ConcurrentTargetControlPump(tmp_path/'pump',client=world['target_client'],worker=w,
            current_member=lambda:world['authority'].coordinator.member(world['member']['node_id']))
        stop=threading.Event();errors=[]
        def run():
            while not stop.is_set():
                try:pump.step()
                except Exception as error:errors.append(type(error).__name__);return
                stop.wait(.01)
        thread=threading.Thread(target=run)
        try:
            w.start();thread.start();yield world,w
        finally:
            stop.set()
            if thread.ident is not None: thread.join(3)
            pump.close();w.close()
        assert not thread.is_alive() and not w.process_group_present()
        assert not errors


@pytest.mark.parametrize('interrupt',[False,True])
def test_independent_control_and_retired_waiter_do_not_block_each_other(tmp_path,monkeypatch,interrupt):
    with connected(tmp_path) as (world,w):
        sender=owner(tmp_path/'sender',world);s=session(sender,world)
        entered=threading.Event();release=threading.Event();original=w.send;replies=[];errors=[]
        def blocked(**kwargs):
            if kwargs['command_id']=='slow':entered.set();release.wait(5)
            return original(**kwargs)
        monkeypatch.setattr(w,'send',blocked)
        def first():
            try:replies.append(s.send(command_id='slow',command='hello',payload={}))
            except ControllerError as error:errors.append(error.code)
        thread=threading.Thread(target=first)
        try:
            thread.start();assert entered.wait(1)
            if interrupt:
                s.interrupt_command('slow','caller_cancelled')
                thread.join(.5)
                assert not thread.is_alive() and errors==['caller_cancelled'] and replies==[]
            reply=s.send_before(command_id='fast',command='hello',payload={},
                deadline_monotonic_s=time.monotonic()+1)
            assert reply['ok'] is True
            assert w.status()['command_count']==1
            release.set();thread.join(3)
            assert not thread.is_alive()
            if interrupt:
                assert sender.reconcile('slow')['ok'] is True
                assert replies==[] # late native result never reaches the retired waiter
            else:assert replies[0]['ok'] is True
            assert w.status()['command_count']==2
        finally:release.set();thread.join(3);sender.close()


def test_expired_absolute_deadline_never_submits(tmp_path,monkeypatch):
    with _world(tmp_path/'swarm') as world:
        sender=owner(tmp_path/'sender',world);s=session(sender,world);calls=[]
        monkeypatch.setattr(world['owner_client'],'enqueue',lambda **kwargs:calls.append(kwargs))
        try:
            with pytest.raises(TimeoutError):
                s.send_before(command_id='expired',command='hello',payload={},deadline_monotonic_s=time.monotonic()-1)
            assert len(calls)==0
        finally:sender.close()
