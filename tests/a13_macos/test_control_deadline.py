# SPDX-License-Identifier: AGPL-3.0-only
"""Owner wait/close regressions; real local TLS, no model or public transport."""
import threading
import time
import pytest
from test_control_pump import _world
from test_control_session import owner
from test_managed_control import RUN_ID, DEPLOYMENT_ID
from physical_inference_qualification import ControllerError


def test_owner_cannot_enqueue_after_waiting_past_deadline_for_signer_lock(tmp_path,monkeypatch):
    with _world(tmp_path/'swarm') as world:
        sender=owner(tmp_path/'sender',world)
        s=sender.session(node_id=world['member']['node_id'],run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID,timeout_seconds=.05)
        calls=[];errors=[];entered=threading.Event()
        original=world['owner_client'].enqueue
        def enqueue(**kwargs):calls.append(kwargs['native_command']['command_id']);return original(**kwargs)
        monkeypatch.setattr(world['owner_client'],'enqueue',enqueue)
        def send():
            entered.set()
            try:s.send(command_id='too-late',command='hello',payload={})
            except ControllerError as error:errors.append(error.code)
        thread=threading.Thread(target=send)
        try:
            with sender._lock:
                thread.start();assert entered.wait(1)
                time.sleep(.15)
            thread.join(timeout=2)
            assert not thread.is_alive()
            assert calls==[]
            assert errors==['managed_control_response_timeout']
        finally:sender.close();thread.join(timeout=2)


def test_owner_cannot_enqueue_after_membership_lookup_exhausts_deadline(tmp_path,monkeypatch):
    with _world(tmp_path/'swarm') as world:
        sender=owner(tmp_path/'sender',world)
        s=sender.session(node_id=world['member']['node_id'],run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID,timeout_seconds=.01)
        original=sender.current_member;calls=[]
        def slow(node_id):time.sleep(.03);return original(node_id)
        monkeypatch.setattr(sender,'current_member',slow)
        monkeypatch.setattr(world['owner_client'],'enqueue',lambda **kwargs:calls.append(kwargs))
        try:
            with pytest.raises(ControllerError,match='response_timeout'):
                s.send(command_id='late-context',command='hello',payload={})
            assert calls==[]
        finally:sender.close()


def test_stalled_real_https_reply_does_not_outlive_owner_budget(tmp_path,monkeypatch):
    from mycelium_a13_macos.managed_control import _ManagedControlRequestHandler
    with _world(tmp_path/'swarm') as world:
        sender=owner(tmp_path/'sender',world)
        s=sender.session(node_id=world['member']['node_id'],run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID,timeout_seconds=.08)
        committed=threading.Event(); release=threading.Event()
        original=_ManagedControlRequestHandler._send
        def stall(handler,status,document):
            if handler.path=='/managed-control/enqueue' and status==200:
                handler.send_response(200)
                handler.send_header('Content-Type','application/json')
                handler.send_header('Content-Length','1000')
                handler.send_header('Connection','close')
                handler.end_headers()
                handler.wfile.write(b'{');handler.wfile.flush()
                committed.set();release.wait(.5)
            else:original(handler,status,document)
        monkeypatch.setattr(_ManagedControlRequestHandler,'_send',stall)
        try:
            began=time.monotonic()
            with pytest.raises(ControllerError):
                s.send(command_id='stalled-reply',command='hello',payload={})
            elapsed=time.monotonic()-began
            assert committed.is_set()
            assert elapsed < .3
        finally:release.set();sender.close()
