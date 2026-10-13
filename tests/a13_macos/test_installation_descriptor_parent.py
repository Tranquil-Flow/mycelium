"""Signed installation metadata; synthetic source fixture, no tensors."""
import copy
from types import SimpleNamespace
import pytest
from mycelium_a13_macos.app import DemoApp
from test_app_authority import CONSENT


def test_installation_facts_bound_to_current_member_and_run(tmp_path,monkeypatch):
    from mycelium_a13_macos.installation_descriptor import installation_descriptor,verify_installation_descriptor
    app=DemoApp(tmp_path/'owner',check_platform=False).start()
    try:
        app.action('/api/create',{'consent':CONSENT});app.action('/api/local-contributor/start',{})
        actor=app.local_contributor;assert actor is not None
        source=app.acquisition.state_root/'fixture';source.mkdir()  # per-model owner cache (run_model.py)
        monkeypatch.setattr(app.acquisition,'prepared_source',lambda:SimpleNamespace(source_root=source,manifest_digest='sha256:'+'1'*64))
        with pytest.raises(ValueError):installation_descriptor(app,run_id='run-one',deployment_id='deployment-one',acknowledged=False)
        card=installation_descriptor(app,run_id='run-one',deployment_id='deployment-one',acknowledged=True)
        member=app.authority.coordinator.member(actor.member.node_id)
        facts=verify_installation_descriptor(card,member=member,swarm_id=app.authority.coordinator.swarm_id,run_id='run-one',deployment_id='deployment-one')
        assert facts['staging_root']==str(actor.peer.state_root/'managed/run-one/mycelium-stage')
        assert facts['source_root']==str(source)
        assert facts['route_ready'] is False
        import base64
        assert facts['activation_endpoint_id']==base64.b64decode(card['verification_key']['verification_key'],validate=True).hex()
        assert facts['endpoint_id']==member['endpoint_id']
        assert 'host_id' not in facts and 'boot_id' not in facts
        assert facts['ssh_target'] is None and facts['process_transport']=='managed_https'
        bad=copy.deepcopy(card);bad['statement']['facts']['staging_root']='/tmp/other'
        with pytest.raises(ValueError):verify_installation_descriptor(bad,member=member,swarm_id=app.authority.coordinator.swarm_id,run_id='run-one',deployment_id='deployment-one')
        with pytest.raises(ValueError):verify_installation_descriptor(card,member={**member,'generation':member['generation']+1},swarm_id=app.authority.coordinator.swarm_id,run_id='run-one',deployment_id='deployment-one')
        assert actor._runtime is None
    finally:app.close()
