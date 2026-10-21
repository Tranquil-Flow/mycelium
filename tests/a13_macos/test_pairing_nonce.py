# SPDX-License-Identifier: AGPL-3.0-only
"""Generated nonces must satisfy the same contract as received requests."""
import pytest
from mycelium_a13_macos import pairing
from mycelium_node.identity import load_or_create_node_signer


def test_generated_nonce_accepts_every_random_prefix(tmp_path,monkeypatch):
    signer=load_or_create_node_signer(tmp_path/'node.key')
    monkeypatch.setattr(pairing.os,'urandom',lambda count:bytes([255])*count)
    request,_=pairing.new_pairing_request(signer=signer,node_id='test-mac',
        peer_class='mac_mlx_iroh',now=1000)
    assert pairing.verify_pairing_request(request,now=1001)==request


@pytest.mark.parametrize('nonce',['',False])
def test_explicit_invalid_nonce_is_not_silently_replaced(tmp_path,nonce):
    signer=load_or_create_node_signer(tmp_path/'node.key')
    with pytest.raises(pairing.PairingError,match='pairing_request_invalid'):
        pairing.new_pairing_request(signer=signer,node_id='test-mac',
            peer_class='mac_mlx_iroh',now=1000,nonce=nonce)
