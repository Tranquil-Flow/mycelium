# SPDX-License-Identifier: AGPL-3.0-only
"""One real canonical TLS seed origin for membership plus managed control."""
from test_managed_control import _world, _enqueue, RUN_ID, DEPLOYMENT_ID
from mycelium_a13_macos.managed_control import ManagedControlClient


def test_managed_control_shares_authority_origin_and_preserves_seed_routes(tmp_path):
    with _world(tmp_path) as world:
        authority=world['authority']
        assert authority.control_mailbox is not None
        material=authority.seed_server.material
        common=dict(base_url=authority.seed_server.base_url,
            trusted_owner_key_record=world['owner_signer'].public_key_record(),
            swarm_id=authority.coordinator.swarm_id,owner_node_id=authority.coordinator.seed_node_id,
            ca_certificate=material.ca_certificate)
        world['owner_client']=ManagedControlClient.for_local_tls(signer=world['owner_signer'],**common)
        world['target_client']=ManagedControlClient.for_local_tls(signer=world['target_signer'],**common)
        assert _enqueue(world,'same-origin-hello')['state']=='pending'
        reply=world['target_client'].poll(target=world['member'],run_id=RUN_ID,deployment_id=DEPLOYMENT_ID,
            sequence=1,nonce='same-origin-nonce',request_id='same-origin-request')
        assert reply['delivery_kind']=='command'
        assert reply['command_envelope']['statement']['native_command']['command']=='hello'
        # The same listener still serves actual canonical A8 membership renewal.
        assert world['target'].renew()['membership_state']=='current'
