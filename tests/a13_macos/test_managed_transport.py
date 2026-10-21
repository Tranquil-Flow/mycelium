# SPDX-License-Identifier: AGPL-3.0-only
"""Managed transport selection is explicit; never silently invokes SSH/argv."""
import pytest
from physical_inference_qualification import PeerIdentity, _peer_process_argv, NodeProcessSession, ControllerError


def peer(**changes):
    value=dict(node_id='managed-node',ssh_target=None,host_id='host-one',boot_id='boot-one',
        staging_root='/private/tmp/mycelium-managed-test',process_transport='managed_https')
    value.update(changes)
    return PeerIdentity(**value)


def test_managed_peer_has_no_ssh_identity_or_launch_argv():
    managed=peer()
    assert _peer_process_argv(managed,('python3','physical_inference_node.py')) == ()
    # Without the explicitly injected managed factory, native launch fails closed.
    with pytest.raises(ControllerError,match='node_process_arguments_invalid'):
        NodeProcessSession(argv=(),node_id=managed.node_id,run_id='run-one',
            deployment_id='deployment-one',timeout_seconds=5)


@pytest.mark.parametrize('changes',[
    {'ssh_target':'user@host.invalid'}, {'ssh_identity_file':'/private/tmp/key'},
])
def test_managed_peer_cannot_smuggle_ssh_authority(changes):
    with pytest.raises(ControllerError,match='managed_peer_ssh_authority_invalid'):
        peer(**changes)


@pytest.mark.parametrize('operation',['prepare','cleanup'])
def test_managed_staging_cannot_fall_back_to_controller_ssh(tmp_path,operation):
    from physical_inference_qualification import QualificationController
    controller=QualificationController(mode='physical',peers=(peer(),),source_root=tmp_path,
        transfer_manifest={},membership_snapshot={},now=1.0)
    # Reject before even reading an artifact, let alone staging/cleanup effects.
    with pytest.raises(ControllerError,match='managed_artifact_lifecycle_requires_target_owner'):
        controller.execute(operation)
