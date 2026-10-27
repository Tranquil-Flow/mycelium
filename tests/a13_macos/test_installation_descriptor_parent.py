# SPDX-License-Identifier: AGPL-3.0-only
"""Signed installation metadata; synthetic source fixture, no tensors."""
import copy
from types import SimpleNamespace
import pytest
from mycelium_a13_macos.app import DemoApp
from test_app_authority import CONSENT


def test_installation_facts_bound_to_current_member_and_run(native_sidecar_required, tmp_path,monkeypatch):
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


def test_each_refusal_names_the_check_that_failed(native_sidecar_required, tmp_path,monkeypatch):
    """Two-Mac run 18b: one collapsed ``installation_descriptor_invalid`` hid an
    expired descriptor. Each refusal is a bounded code naming the failed check."""
    from mycelium_a13_macos.installation_descriptor import installation_descriptor,verify_installation_descriptor
    app=DemoApp(tmp_path/'owner',check_platform=False).start()
    try:
        app.action('/api/create',{'consent':CONSENT});app.action('/api/local-contributor/start',{})
        actor=app.local_contributor
        source=app.acquisition.state_root/'fixture';source.mkdir()
        monkeypatch.setattr(app.acquisition,'prepared_source',lambda:SimpleNamespace(source_root=source,manifest_digest='sha256:'+'1'*64))
        card=installation_descriptor(app,run_id='run-one',deployment_id='deployment-one',acknowledged=True)
        issued,expires=card['statement']['issued_at'],card['statement']['expires_at']
        swarm=app.authority.coordinator.swarm_id
        member=app.authority.coordinator.member(actor.member.node_id)
        live={**member,'lease_expires_at':issued+3600}  # a renewed lease (the app renews every 20 s)
        def code(card=card,member=live,swarm_id=swarm,run_id='run-one',now=None):
            with pytest.raises(ValueError) as info:
                verify_installation_descriptor(card,member=member,swarm_id=swarm_id,run_id=run_id,
                                               deployment_id='deployment-one',now=now)
            return str(info.value)
        assert verify_installation_descriptor(card,member=live,swarm_id=swarm,run_id='run-one',
                                              deployment_id='deployment-one',now=expires-1)['route_ready'] is False
        # Run 18b: the owner's own descriptor, issued 51 s in, verified 1585 s in.
        assert code(now=issued+1534)=='installation_descriptor_expired'
        assert code(now=issued-120)=='installation_descriptor_from_future'
        assert code(member={**live,'lease_expires_at':issued+10},now=issued+20)=='installation_descriptor_member_lease_expired'
        assert code(member={**live,'generation':live['generation']+1})=='installation_descriptor_generation_mismatch'
        assert code(member={**live,'incarnation':'remote-incarnation-other'})=='installation_descriptor_generation_mismatch'
        assert code(member={**live,'verification_key_digest':'sha256:'+'9'*64})=='installation_descriptor_key_mismatch'
        assert code(swarm_id='swarm-foreign')=='installation_descriptor_binding_mismatch'
        assert code(run_id='run-other')=='installation_descriptor_binding_mismatch'
        bad=copy.deepcopy(card);bad['statement']['facts']['staging_root']='/tmp/other'
        assert code(card=bad)=='installation_descriptor_signature_invalid'
        bad=copy.deepcopy(card);del bad['statement']
        assert code(card=bad)=='installation_descriptor_malformed'
        # Bounded codes only: no path, key, id or time value is echoed.
        for value in (code(now=issued+1534),code(card=bad)):
            assert value.startswith('installation_descriptor_') and str(source) not in value and swarm not in value
    finally:app.close()
