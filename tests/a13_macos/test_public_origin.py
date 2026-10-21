# SPDX-License-Identifier: AGPL-3.0-only
"""Advertised public seed identity with verified private owner TLS; no public listener."""
import socket
import pytest
from mycelium_a13_macos.authority import DemoAuthority, DemoAuthorityError
from mycelium_a13_macos.tls import pinned_seed_client, LocalTLSError

CONSENT={'cooperative_peer_visibility_acknowledged':True,'resource_use_acknowledged':True,'contribution_enabled':False}
QUOTA={'storage_bytes':2_000_000_000,'network_bytes_per_day':2_000_000_000,'background_allowed':False}


def test_public_origin_survives_owner_enrollment_and_restart_over_private_tls(tmp_path):
    root=tmp_path/'authority';authority=DemoAuthority(root)
    public='https://a13-fixture.invalid'
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
    authority.start()
    try:
        authority.configure_public_endpoint(public_origin=public,port=port,acknowledged=True)
        authority.create_swarm(CONSENT)
        assert authority.seed_server.base_url==f'https://127.0.0.1:{port}'
        assert authority.coordinator.seed_url==public
        request=authority.pair_request()
        handoff=authority.approve_pairing(request,ttl_seconds=120,quota=QUOTA)
        authority.accept_pairing(handoff)
        assert authority.seed_client.seed_url==public
        assert authority.status()['membership_state']=='current'
        key=authority.member.signer.verification_key_digest
        seed_key=authority.coordinator.signer.verification_key_digest
        with pytest.raises(DemoAuthorityError,match='network_configuration_requires_empty_authority'):
            authority.configure_public_endpoint(public_origin='https://other.invalid',port=port,acknowledged=True)
        authority.stop()
        authority=DemoAuthority(root);authority.start()
        assert authority.coordinator.seed_url==public
        assert authority.member.signer.verification_key_digest==key
        assert authority.coordinator.signer.verification_key_digest==seed_key
        assert authority.status()['membership_state']=='current'
    finally:authority.stop()


def test_exposure_configuration_requires_consent_and_safe_port(tmp_path):
    authority=DemoAuthority(tmp_path/'authority');authority.start()
    try:
        for origin,port,ack in [('https://fixture.invalid',55000,False),('http://fixture.invalid',55000,True),('https://fixture.invalid',8791,True)]:
            with pytest.raises(DemoAuthorityError):
                authority.configure_public_endpoint(public_origin=origin,port=port,acknowledged=ack)
        assert not (authority.state_root/'network.json').exists()
    finally:authority.stop()


def test_private_transport_cannot_redirect_owner_to_another_host(tmp_path):
    authority=DemoAuthority(tmp_path/'authority');authority.start();authority.create_swarm(CONSENT)
    try:
        coordinator=authority.coordinator
        with pytest.raises(LocalTLSError,match='local_tls_transport_invalid'):
            pinned_seed_client(seed_url='https://a13-fixture.invalid',swarm_id=coordinator.swarm_id,
                seed_key_digest=coordinator.signer.verification_key_digest,
                seed_key_records=[coordinator.signer.public_key_record()],
                ca_certificate=authority.seed_server.material.ca_certificate,
                transport_url='https://untrusted.invalid')
    finally:authority.stop()
