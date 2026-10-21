# SPDX-License-Identifier: AGPL-3.0-only
"""Loopback proxy preserves canonical signed bytes and TLS validation."""
import json
from urllib.request import urlopen
from urllib.error import HTTPError
import pytest
from mycelium_a13_macos.authority import DemoAuthority


def test_relay_proxy_forwards_seed_identity_and_blocks_other_surfaces(tmp_path):
    from mycelium_a13_macos.public_relay import SeedRelayProxy
    authority=DemoAuthority(tmp_path/'owner');authority.start()
    proxy=SeedRelayProxy().start()
    try:
        with pytest.raises(HTTPError) as error:urlopen(proxy.origin+'/seed/identity',timeout=3)
        assert error.value.code==503
        authority.create_swarm({'cooperative_peer_visibility_acknowledged':True,
            'resource_use_acknowledged':True,'contribution_enabled':False})
        proxy.bind_seed(authority.seed_server)
        with urlopen(proxy.origin+'/seed/identity',timeout=3) as response:document=json.load(response)
        import time
        from mycelium_seed.http import SEED_IDENTITY_PROTOCOL
        from mycelium_a13_macos.tls import pinned_seed_client
        coordinator=authority.coordinator
        client=pinned_seed_client(seed_url=coordinator.seed_url,swarm_id=coordinator.swarm_id,
            seed_key_digest=coordinator.signer.verification_key_digest,
            seed_key_records=[coordinator.signer.public_key_record()],
            ca_certificate=authority.seed_server.material.ca_certificate)
        statement=client._verify_seed_envelope(document,
            expected_protocol=SEED_IDENTITY_PROTOCOL,now=time.time())
        assert statement['swarm_id']==authority.coordinator.swarm_id
        assert statement['seed_url']==authority.coordinator.seed_url
        for path in ['/api/status','/native-session','/seed/../api/status']:
            with pytest.raises(HTTPError) as error:urlopen(proxy.origin+path,timeout=3)
            assert error.value.code==404
    finally:proxy.close();authority.stop()
    assert not proxy.thread.is_alive()


def test_public_relay_requires_explicit_approval_before_process(tmp_path):
    from mycelium_a13_macos.public_relay import TemporaryHttpsRelay
    relay=TemporaryHttpsRelay(tmp_path/'relay')
    with pytest.raises(ValueError,match='relay_approval_required'):
        relay.start(acknowledged=False,max_seconds=1800)
    assert relay.process is None
