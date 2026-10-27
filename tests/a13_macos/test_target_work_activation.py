# SPDX-License-Identifier: AGPL-3.0-only
"""Target offer activation with real signed TLS/native hello, no model load."""
import hashlib
from pathlib import Path
import pytest
from test_control_pump import _world
from test_control_session import owner, session
from test_managed_control import RUN_ID, DEPLOYMENT_ID
from test_managed_staging import bundle
from test_worker import POLICY, SAFE
from mycelium_a13_macos.target_app import TargetApplication
from mycelium_a13_macos.worker import WorkerLimits, WorkerError
from mycelium_a13_macos.authority import _private_directory


def setup(w):
    app=TargetApplication(w['target'].state_root);app.peer=w['target']
    cache=_private_directory(app.peer.state_root/'serving',create=True)
    source=cache/'inert';source.write_bytes(b'not model weights')
    row={'source_path':str(source),'destination_path':'deployment/inert',
         'size_bytes':source.stat().st_size,
         'content_digest':'sha256:'+hashlib.sha256(source.read_bytes()).hexdigest()}
    archive,_=bundle()
    offer=w['authority']._control_mailbox.publish_offer(target=w['member'],run_id=RUN_ID,
        deployment_id=DEPLOYMENT_ID,archive=archive,
        preposition={'protocol':'mycelium.controller_prepositioned_member_artifacts.v1','files':[row]})
    options=dict(acknowledged=True,resource_policy=POLICY,
        limits=WorkerLimits(max_rss_bytes=512*1024*1024,max_disk_bytes=100_000,
                           max_wall_seconds=30,max_commands=20),
        authorize_load=lambda envelope:False,_test_observer=lambda:dict(SAFE),
        _test_ca_certificate=w['authority'].seed_server.material.ca_certificate)
    return app,offer,source,options


def test_work_activation_preserves_preposition_and_exact_load_guard(native_sidecar_required, tmp_path):
    with _world(tmp_path/'world') as w:
        app,offer,source,options=setup(w)
        sender=owner(tmp_path/'sender',w)
        seen=[]
        def deny(envelope):
            seen.append(envelope)
            return False
        options['authorize_load']=deny
        try:
            status=app.start_work(offer,**options)
            assert status['state']=='running' and status['route_ready'] is False
            assert status['model_load_authorized'] is False
            runtime=app._runtime
            assert runtime._marker['preposition_digest']==offer['preposition_digest']
            assert (runtime.staging.staging_root/'deployment/inert').read_bytes()==source.read_bytes()
            native=session(sender,w)
            reply=native.send(command_id='work-hello',command='hello',payload={})
            assert reply['ok'] is True
            # Direct worker rejection demonstrates no native configure/model call.
            with pytest.raises(WorkerError,match='model_execution_not_authorized'):
                runtime.worker.send(command_id='denied-configure',command='configure',payload={})
            assert seen[0]['run_id']==RUN_ID and seen[0]['deployment_id']==DEPLOYMENT_ID
            assert seen[0]['command']=='configure'
            native.close()
        finally:
            app.stop_control();sender.close()
        assert not runtime.worker.process_group_present()
        assert source.read_bytes()==b'not model weights'
        assert app.peer.status()['identity_state']=='present'


@pytest.mark.parametrize('case',['no-consent','no-authorizer','bad-limits'])
def test_invalid_work_activation_has_no_staging_or_process_side_effect(tmp_path,case):
    with _world(tmp_path/'world') as w:
        app,offer,source,options=setup(w)
        if case=='no-consent':options['acknowledged']=False
        elif case=='no-authorizer':options['authorize_load']=None
        else:options['limits']={}
        with pytest.raises(ValueError):app.start_work(offer,**options)
        assert app._runtime is None
        assert not (app.peer.state_root/'managed'/RUN_ID).exists()
        assert source.read_bytes()==b'not model weights'
