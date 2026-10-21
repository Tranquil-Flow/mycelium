# SPDX-License-Identifier: AGPL-3.0-only
"""Host observations and signed self-report are not serving qualification."""
import pytest
import time
from mycelium_membership.contracts import CAPABILITY_REPORT_PROTOCOL, verify_membership_message
import mycelium_a13_macos.capability as capability
from mycelium_a13_macos.authority import DemoAuthority
from mycelium_a13_macos.capability import observe_mac, signed_mac_report

CONSENT={'cooperative_peer_visibility_acknowledged':True,'resource_use_acknowledged':True,'contribution_enabled':False}


def test_observations_fail_closed_and_do_not_probe_a_model(tmp_path):
    with pytest.raises(ValueError,match='unsupported_mac'):
        observe_mac(tmp_path, system='Linux', architecture='x86_64')
    value=observe_mac(tmp_path)
    assert value['platform']=='macos' and value['architecture']=='arm64'
    assert value['memory_bytes']>0 and value['available_storage_bytes']>0
    assert value['independently_qualified'] is False
    assert value['backends']==['mlx'] and value['precisions']==['float32']
    assert value['runtime_evidence']=='local_arithmetic_self_probe'


def test_signed_current_generation_report_is_only_self_reported(tmp_path):
    root=tmp_path/'authority';a=DemoAuthority(root);a.start()
    try:
        a.create_swarm(CONSENT)
        req=a.pair_request();handoff=a.approve_pairing(req,ttl_seconds=120,quota={
            'storage_bytes':1_000_000_000,'network_bytes_per_day':1_000_000_000,'background_allowed':False})
        a.accept_pairing(handoff)
        report=signed_mac_report(a.member,root)
        assert report['message']['generation']==a.member.generation
        assert report['message']['protocol']==CAPABILITY_REPORT_PROTOCOL
        verify_membership_message(report,now=time.time(),
            expected_key_digest=a.member.signer.verification_key_digest,
            expected_protocol=CAPABILITY_REPORT_PROTOCOL,
            expected_sender_node_id=a.member.node_id)
        assert report['message']['backends']==['mlx']
        assert 'signature' in report
        assert a.status()['route_ready'] is False
    finally:a.stop()


def test_unavailable_backend_is_not_fabricated_or_signed(tmp_path,monkeypatch):
    monkeypatch.setattr(capability,'probe_mlx',lambda:False,raising=False)
    value=observe_mac(tmp_path)
    assert value['backends']==[] and value['precisions']==[]
    assert value['runtime_evidence']=='unavailable'
    class Member:
        generation=1
        def capability_report(self,**kwargs):
            raise AssertionError('must_not_sign_an_unsupported_backend')
    with pytest.raises(ValueError,match='capability_backend_unavailable'):
        signed_mac_report(Member(),tmp_path)
