# SPDX-License-Identifier: AGPL-3.0-only
"""Installed target host pins come only from accepted join/client state."""
import json
import time
from types import SimpleNamespace
import pytest

from mycelium_qualification.signing import generate_ed25519_signer


def test_target_host_pin_uses_join_owner_and_never_restarts_process_budget(tmp_path):
    from mycelium_a13_macos import token_policy
    owner=generate_ed25519_signer(endpoint_id='owner-control')
    target=generate_ed25519_signer(endpoint_id='target-control')
    client=SimpleNamespace(signer=target,trusted_owner_key_record=owner.public_key_record(),
        trusted_owner_endpoint_id=owner.endpoint_id)
    member=dict(node_id='target',endpoint_id=target.endpoint_id,
        verification_key_digest=target.verification_key_digest,generation=3,lease_expires_at=time.time()+60)
    options=dict(root=tmp_path,client=client,current_member=lambda:member,run_id='run',
        deployment_id='deployment',max_wall_seconds=60)
    path=token_policy.prepare_host_pin(**options)
    before=path.read_bytes()
    body=json.loads(before)
    assert body['owner_key']==owner.public_key_record()
    assert body['owner_endpoint_id']==owner.endpoint_id
    assert body['member']==dict(node_id='target',endpoint_id=target._public_key_bytes.hex(),generation=3,
        control_endpoint_id=target.endpoint_id,verification_key=target.public_key_record())
    assert path.stat().st_mode & 0o777 == 0o600
    assert token_policy.prepare_host_pin(**options)==path
    assert path.read_bytes()==before
    member['generation']+=1
    with pytest.raises((RuntimeError,ValueError)):
        token_policy.prepare_host_pin(**options)


def test_joined_target_application_provisions_native_pin_and_cursor(tmp_path):
    from test_managed_runtime import runtime, _world, RUN_ID, DEPLOYMENT_ID, POLICY, SAFE
    from mycelium_a13_macos.target_app import TargetApplication
    with _world(tmp_path/'world') as world:
        old=runtime(tmp_path/'target',world)
        app=TargetApplication(tmp_path/'application',_test_peer_factory=lambda root:world['target'])
        managed=None
        try:
            managed=app.prepare_runtime(run_id=RUN_ID,deployment_id=DEPLOYMENT_ID,
                staging=old.staging,archive=old._archive,archive_digest=old.archive_digest,
                resource_policy=POLICY,limits=old.worker.limits,sidecar_binary=tmp_path/'never-started',
                authorize_load=None,_test_observer=lambda:dict(SAFE),
                _test_ca_certificate=world['authority'].seed_server.material.ca_certificate)
            assert managed.worker.token_policy_trust_file is not None
            pin=json.loads(managed.worker.token_policy_trust_file.read_bytes())
            assert pin['owner_key']==world['owner_signer'].public_key_record()
            assert callable(managed.pump._token_progress.factory)
            with pytest.raises((RuntimeError,ValueError),match='token_path_activation_unprovisioned'):
                managed.pump._token_progress.factory({})
            assert app.activate_runtime(managed)['state']=='running'
            assert not managed.worker.status()['model_load_authorized']
        finally:
            app.stop()
            if managed is not None:managed.close()
            old.close()


def test_target_without_independent_owner_endpoint_pin_stays_unprovisioned(tmp_path):
    from mycelium_a13_macos import token_policy
    client=SimpleNamespace(trusted_owner_endpoint_id=None)
    assert token_policy.prepare_host_pin(root=tmp_path,client=client,current_member=lambda:None,
        run_id='run',deployment_id='deployment',max_wall_seconds=60) is None
    assert not tuple(tmp_path.iterdir())
