# SPDX-License-Identifier: AGPL-3.0-only
"""Ordering contract for the local-only signed receipt transport fixture."""
import json
import threading
from types import SimpleNamespace
from mycelium_qualification.signing import generate_ed25519_signer
from tests.live.local_token_authority import NativeProgress


def test_sealed_receipt_is_visible_before_ack_releases_native_completion():
    ack_entered, release_ack = threading.Event(), threading.Event()
    signer = generate_ed25519_signer(endpoint_id='owner')
    scope = dict(entry_node_id='entry',entry_endpoint_id='entry-endpoint',
        entry_key_digest='sha256:'+'a'*64,participants=[dict(node_id='entry',generation=1)],
        run_id='run',control=dict(deployment_id='deployment'))
    receipt = SimpleNamespace(sequence=0,token_ids=(101,),status='COMPLETED',
        receipt_digest='sha256:'+'b'*64,envelope_bytes=b'{"private_test_receipt":true}')
    cursor = SimpleNamespace(_fence=lambda:None,
        grant=SimpleNamespace(statement=lambda:dict(scope=scope),digest='sha256:'+'c'*64,
            deadline_monotonic_s=10**12))
    sealed=[]
    def send(**kwargs):
        assert sealed==[receipt.envelope_bytes]
        assert set(kwargs['payload'])=={'ack'}
        ack_entered.set()
        assert release_ack.wait(3)
        return dict(ok=True)
    process=SimpleNamespace(open_progress=lambda **kw:SimpleNamespace(read=lambda:receipt),
        close_progress=lambda command_id:None)
    stream=NativeProgress(SimpleNamespace(_process=process,send=send),'start',cursor,sealed.append,signer)
    try:
        assert ack_entered.wait(1)
        # Native infer_start may complete while its ACK RPC is still returning.
        # A reader must already have the verified, sealed terminal at that point.
        assert stream.read(timeout=.05) is receipt
    finally:
        release_ack.set()
        stream.close()


def test_off_mode_does_not_provision_a_token_authority(monkeypatch):
    from tests.live import local_token_authority as fixture
    def forbidden(**_kwargs):
        raise AssertionError('OFF_MUST_NOT_GENERATE_TOKEN_IDENTITY')
    monkeypatch.setattr(fixture, 'generate_ed25519_signer', forbidden)
    for setting in (None, 'off'):
        if setting is None:
            monkeypatch.delenv('MYCELIUM_TOKEN_PATH', raising=False)
        else:
            monkeypatch.setenv('MYCELIUM_TOKEN_PATH', setting)
        # No local metadata is needed or touched on the legacy path.
        authority = fixture.LocalTokenAuthority(SimpleNamespace())
        assert authority.owner is None
        assert authority.files == {}


def test_on_mode_still_requires_the_signed_authority_path(monkeypatch):
    import pytest
    from tests.live import local_token_authority as fixture
    def reached(**_kwargs):
        raise RuntimeError('ON_SIGNER_REACHED')
    monkeypatch.setenv('MYCELIUM_TOKEN_PATH', 'on')
    monkeypatch.setattr(fixture, 'generate_ed25519_signer', reached)
    with pytest.raises(RuntimeError, match='ON_SIGNER_REACHED'):
        fixture.LocalTokenAuthority(SimpleNamespace())
