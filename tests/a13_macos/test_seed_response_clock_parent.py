# SPDX-License-Identifier: AGPL-3.0-only
"""Receive-time sampling seam; signature validation is not mocked as proof."""
import pytest
from mycelium_seed.http import SeedHTTPClient


@pytest.mark.parametrize('operation',['identity','receipt'])
def test_response_validation_samples_clock_after_transport(operation):
    client=SeedHTTPClient.__new__(SeedHTTPClient);events=[];clock_value=[100.0]
    def request(*args):
        events.append('transport');clock_value[0]=103.0
        return {'fixture':'unverified'}
    def clock():events.append('clock');return clock_value[0]
    def verify(response,**kwargs):
        assert events==['transport','clock']
        assert kwargs['now']==103.0
        return {'accepted_message_id':'message-1'}
    client._request=request;client._verify_seed_envelope=verify
    if operation=='identity':client.identity(now=clock)
    else:client.send_member_message({'message':{'message_id':'message-1','protocol':'fixture'}},now=clock)


def test_signed_http_delayed_identity_and_expiry_are_enforced(tmp_path):
    from mycelium_seed import SeedCoordinator
    from mycelium_seed.http import SeedHTTPServer,SeedHTTPError
    from mycelium_invite import SqliteInviteRegistry
    from mycelium_qualification.signing import generate_ed25519_signer
    current=[3000.0]
    coordinator=SeedCoordinator(swarm_id='clock-test',seed_node_id='seed-clock',seed_url=None,
        signer=generate_ed25519_signer(endpoint_id='clock-endpoint'),
        invite_registry=SqliteInviteRegistry(tmp_path/'invites.sqlite3'),
        incarnation='clock-incarnation',clock=lambda:current[0])
    with SeedHTTPServer(coordinator,host='127.0.0.1',port=0) as server:
        bundle=coordinator.mint_invite(nonce='clock-invite',ttl_seconds=120)
        client=SeedHTTPClient.from_invite_bundle(bundle,now=current[0],timeout=2)
        request=client._request
        def delayed(*args):
            current[0]=3033.0
            return request(*args)
        client._request=delayed
        # Beyond MAX_CLOCK_SKEW_SECONDS (30 s) behind the issuer: rejected.
        with pytest.raises(SeedHTTPError,match='seed_http_seed_time_invalid'):
            client.identity(now=3001.0)
        # Within it (2 s behind, the class two-Mac run #19 hit at 0.18 s): accepted.
        assert client.identity(now=3031.0)['seed_endpoint_id']=='clock-endpoint'
        assert client.identity(now=lambda:current[0])['seed_endpoint_id']=='clock-endpoint'
        with pytest.raises(SeedHTTPError,match='seed_http_seed_time_invalid'):
            client.identity(now=lambda:10000.0)
