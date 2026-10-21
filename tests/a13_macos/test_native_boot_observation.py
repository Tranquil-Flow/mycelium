# SPDX-License-Identifier: AGPL-3.0-only
"""Real native hello exposes only the already-derived run-scoped boot ID."""
from test_control_pump import _world
from test_control_session import owner, session
from test_managed_runtime import runtime as make_runtime
from test_managed_control import RUN_ID


def test_signed_native_hello_retains_derived_boot_identity(tmp_path):
    from mycelium_physical_runner.remote_probe import derive_local_run_scoped_identity
    with _world(tmp_path/'world') as w:
        runtime=make_runtime(tmp_path/'runtime',w)
        sender=owner(tmp_path/'owner',w)
        try:
            runtime.start()
            native=session(sender,w)
            reply=native.send(command_id='boot-identity-observation',command='hello',payload={})
            assert reply['ok'] is True
            host,boot=derive_local_run_scoped_identity(RUN_ID)
            assert reply['result']['host_id']==host
            assert reply['result']['boot_id']==boot
            assert reply['result']['route_ready'] is False
            native.close()
        finally:
            runtime.close();sender.close()


def test_qualification_hello_requires_current_managed_boot(tmp_path):
    import time
    import pytest
    from physical_inference_qualification import PeerIdentity, QualificationController, ControllerError
    with _world(tmp_path/'world') as w:
        runtime=make_runtime(tmp_path/'runtime',w)
        sender=owner(tmp_path/'owner',w)
        try:
            runtime.start()
            native=session(sender,w)
            reply=native.send(command_id='boot-qualification-contract',command='hello',payload={})
            identity=reply['result']
            peer=PeerIdentity(node_id=w['member']['node_id'],ssh_target=None,
                host_id=identity['host_id'],boot_id=identity['boot_id'],
                staging_root='/private/tmp/mycelium-managed-boot-test',process_transport='managed_https')
            verifier=QualificationController(mode='dry-run',peers=(peer,),source_root=tmp_path,
                transfer_manifest={},membership_snapshot={},now=time.time())
            got=verifier._hello_identity(reply,peer=peer,run_id=RUN_ID,
                deployment_id=identity['deployment_id'])
            assert got['boot_id']==peer.boot_id
            for bad in ({k:v for k,v in identity.items() if k!='boot_id'},
                        {**identity,'boot_id':'boot-'+'0'*32},
                        {**identity,'unrecognized_field':'not-allowed'}):
                with pytest.raises(ControllerError,match='node_hello_identity_invalid'):
                    verifier._hello_identity({**reply,'result':bad},peer=peer,run_id=RUN_ID,
                        deployment_id=identity['deployment_id'])
            native.close()
        finally:
            runtime.close();sender.close()
