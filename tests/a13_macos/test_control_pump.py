# SPDX-License-Identifier: AGPL-3.0-only
"""Actual encrypted TLS membership/control -> native subprocess; no model."""
from pathlib import Path
import pytest
from test_managed_control import _world as _separate_world, _enqueue, RUN_ID, DEPLOYMENT_ID
from contextlib import contextmanager
from mycelium_a13_macos.managed_control import ManagedControlClient
from test_worker import worker
from mycelium_a13_macos.control_pump import TargetControlPump, ControlPumpError

@contextmanager
def _world(path):
    # Exercise the actual authority's single TLS listener, not a separate API server.
    with _separate_world(path) as world:
        authority=world['authority']
        common=dict(base_url=authority.seed_server.base_url,
            trusted_owner_key_record=world['owner_signer'].public_key_record(),
            swarm_id=authority.coordinator.swarm_id,owner_node_id=authority.coordinator.seed_node_id,
            ca_certificate=authority.seed_server.material.ca_certificate)
        world['owner_client']=ManagedControlClient.for_local_tls(signer=world['owner_signer'],**common)
        world['target_client']=ManagedControlClient.for_local_tls(signer=world['target_signer'],**common)
        yield world


def test_real_target_poll_runs_native_hello_and_signed_ack(tmp_path):
    with _world(tmp_path/'swarm') as world:
        root=tmp_path/'native';root.mkdir()
        w=worker(root,node_id=world['member']['node_id'],run_id=RUN_ID,deployment_id=DEPLOYMENT_ID)
        pump=TargetControlPump(tmp_path/'pump',client=world['target_client'],worker=w,
            current_member=lambda:world['authority'].coordinator.member(world['member']['node_id']))
        try:
            w.start();_enqueue(world,'native-hello')
            result=pump.step()
            assert result['state']=='completed'
            assert result['native_response']['ok'] is True
            assert result['native_response']['result']  # native hello, not a synthetic response
            assert result['ack']['state']=='completed'
            assert result['native_response']['route_ready'] is False
            assert pump.step()['state']=='empty'
            assert w.status()['command_count']==1
        finally:pump.close();w.close()
        assert not w.process_group_present()

def test_load_denial_is_not_fabricated_native_evidence(tmp_path):
    with _world(tmp_path/'swarm') as world:
        root=tmp_path/'native';root.mkdir()
        w=worker(root,node_id=world['member']['node_id'],run_id=RUN_ID,deployment_id=DEPLOYMENT_ID)
        pump=TargetControlPump(tmp_path/'pump',client=world['target_client'],worker=w,
            current_member=lambda:world['authority'].coordinator.member(world['member']['node_id']))
        try:
            # Shape-valid command reaches the local grant gate; no artifact read.
            w.start();_enqueue(world,'denied-load',operation='configure',payload={
                'assignment_file':'assignment.json','artifact_report_file':'report.json',
                'graph':{},'device_states':{},'load_generation':1})
            result=pump.step()
            # No native invocation means no invented native failure receipt.
            assert result['state']=='reconciliation_required'
            assert result['native_response'] is None
            assert result['error_code']=='model_execution_not_authorized'
            assert w.status()['command_count']==0
        finally:pump.close();w.close()

def test_lost_ack_never_reexecutes_native_command_on_restart(tmp_path,monkeypatch):
    with _world(tmp_path/'swarm') as world:
        root=tmp_path/'native';root.mkdir()
        w=worker(root,node_id=world['member']['node_id'],run_id=RUN_ID,deployment_id=DEPLOYMENT_ID)
        state=tmp_path/'pump'
        current=lambda:world['authority'].coordinator.member(world['member']['node_id'])
        pump=TargetControlPump(state,client=world['target_client'],worker=w,current_member=current)
        original=world['target_client'].ack
        def lose_ack(**kwargs):
            original(**kwargs)
            raise TimeoutError('response deliberately lost after real successful ack')
        try:
            w.start();_enqueue(world,'one-real-hello')
            monkeypatch.setattr(world['target_client'],'ack',lose_ack)
            with pytest.raises(ControlPumpError,match='control_ack_reconciliation_required'):pump.step()
            assert w.status()['command_count']==1
            pump.close()
            monkeypatch.setattr(world['target_client'],'ack',original)
            world['target_client']._prepared_requests.clear()
            resumed=TargetControlPump(state,client=world['target_client'],worker=w,current_member=current)
            assert resumed.step()['state']=='completed'
            assert w.status()['command_count']==1
            assert resumed.step()['state']=='empty'
            resumed.close()
        finally:pump.close();w.close()
