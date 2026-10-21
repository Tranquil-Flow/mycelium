# SPDX-License-Identifier: AGPL-3.0-only
"""Stop reconciliation uses the original native command, never a second stop."""
import threading
from test_control_session import connected,owner,session


def test_concurrent_close_issues_one_stop(tmp_path,monkeypatch):
    with connected(tmp_path) as (world,w):
        sender=owner(tmp_path/'sender',world)
        s=session(sender,world);original=s.send
        entered=threading.Event();duplicate=threading.Event();release=threading.Event();calls=[]
        def send(**kwargs):
            calls.append(kwargs['command_id'])
            if len(calls)>1:duplicate.set()
            entered.set();release.wait(2)
            return original(**kwargs)
        monkeypatch.setattr(s,'send',send)
        a=threading.Thread(target=s.close);b=threading.Thread(target=s.close)
        try:
            a.start();assert entered.wait(1);b.start()
            duplicate.wait(.15);release.set()
            a.join(3);b.join(3)
            assert not a.is_alive() and not b.is_alive()
            assert len(calls)==1
            assert w.status()['command_count']==1
            assert s.stop_response['ok'] is True
        finally:release.set();a.join(3);b.join(3);sender.close()


def test_owner_restart_reconciles_lost_stop_result_without_second_stop(tmp_path,monkeypatch):
    with connected(tmp_path) as (world,w):
        sender=owner(tmp_path/'sender',world);s=session(sender,world)
        original=world['owner_client'].result
        def lose(**kwargs):
            reply=original(**kwargs)
            if reply['state']=='completed':raise TimeoutError('lost real stop response')
            return reply
        try:
            monkeypatch.setattr(world['owner_client'],'result',lose)
            s.close()
            assert s.cleanup_error is not None
            assert w.status()['command_count']==1
            stop_id=s.stop_command_id
            sender.close()
            monkeypatch.setattr(world['owner_client'],'result',original)
            sender=owner(tmp_path/'sender',world)
            reply=sender.reconcile(stop_id)
            assert reply['ok'] is True
            assert w.status()['command_count']==1
        finally:sender.close()
