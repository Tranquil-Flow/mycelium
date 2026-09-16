"""Installed target lifecycle over real loopback TLS/native hello; never inference."""
import time
import pytest
from test_control_pump import _world
from test_control_session import owner, session
from test_managed_staging import bundle
from test_worker import POLICY, SAFE
from mycelium_a13_macos.worker import WorkerLimits
from mycelium_a13_macos.managed_runtime import ManagedTargetRuntime, RuntimeErrorCode
from mycelium_a13_macos.managed_staging import TargetStaging
from test_managed_control import RUN_ID, DEPLOYMENT_ID


def runtime(tmp_path, world, **kw):
    current=lambda:world['authority'].coordinator.member(world['member']['node_id'])
    node=world['member']['node_id']
    stage=TargetStaging(tmp_path/'mycelium-staging',node_id=node,max_archive_bytes=100_000,
        max_disk_bytes=2_000_000,current_member=lambda:True,authorize=lambda intent:True)
    data,digest=bundle()
    stage.prepare(archive=data,archive_digest=digest)
    args=dict(state_root=tmp_path/'runtime',client=world['target_client'],current_member=current,
        run_id=RUN_ID,deployment_id=DEPLOYMENT_ID,staging=stage,
        archive=data,archive_digest=digest,resource_policy=POLICY,
        limits=WorkerLimits(max_rss_bytes=2_000_000_000,max_disk_bytes=2_000_000,max_wall_seconds=30,max_commands=16),
        sidecar_binary=tmp_path/'never-started',authorize_load=None,_test_observer=lambda:dict(SAFE))
    args.update(kw)
    return ManagedTargetRuntime(**args)


def test_target_owned_staging_pump_native_hello_and_cleanup(tmp_path):
    with _world(tmp_path/'world') as world:
        target=runtime(tmp_path/'target',world)
        sender=owner(tmp_path/'owner',world)
        try:
            result=target.start()
            assert result['state']=='running'
            assert result['route_ready'] is False
            native=session(sender,world)
            reply=native.send(command_id='runtime-hello',command='hello',payload={})
            assert reply['ok'] is True and reply['result']
            assert reply['route_ready'] is False
            native.close()
            assert native.stop_response['ok'] is True
            target.close(cleanup=True)
            assert not target.staging.staging_root.exists()
            assert not target.worker.process_group_present()
            assert target.status()['state']=='closed'
        finally:
            target.close(); sender.close()


def test_revocation_stops_pump_and_process_preserves_stage(tmp_path):
    with _world(tmp_path/'world') as world:
        current=[world['authority'].coordinator.member(world['member']['node_id'])]
        target=runtime(tmp_path/'target',world,current_member=lambda:current[0])
        try:
            target.start(); current[0]=None
            deadline=time.monotonic()+6
            while target.status()['state']=='running' and time.monotonic()<deadline: time.sleep(.05)
            assert target.status()['state']=='blocked'
            assert not target.worker.process_group_present()
            assert target.staging.staging_root.exists()
        finally: target.close()


def test_target_application_stop_drains_attached_runtime_before_membership(tmp_path):
    from mycelium_a13_macos.target_app import TargetApplication
    with _world(tmp_path/'world') as world:
        target=runtime(tmp_path/'target',world)
        app=TargetApplication(tmp_path/'application',_test_peer_factory=lambda root:world['target'])
        try:
            assert app.activate_runtime(target)['state']=='running'
            app.stop()
            assert target.status()['state']=='closed'
            assert not target.worker.process_group_present()
            assert world['target'].status()['identity_state']=='present'
        finally: target.close()


def test_target_runtime_uses_joined_peer_not_owner_membership_lookup(tmp_path):
    from mycelium_a13_macos.target_app import TargetApplication
    with _world(tmp_path/'world') as world:
        old=runtime(tmp_path/'target',world)
        app=TargetApplication(tmp_path/'application',_test_peer_factory=lambda root:world['target'])
        sender=owner(tmp_path/'owner',world)
        try:
            managed=app.prepare_runtime(run_id=RUN_ID,deployment_id=DEPLOYMENT_ID,
                staging=old.staging,archive=old._archive,archive_digest=old.archive_digest,
                resource_policy=POLICY,limits=old.worker.limits,
                sidecar_binary=tmp_path/'never-started',authorize_load=None,
                _test_observer=lambda:dict(SAFE),
                _test_ca_certificate=world['authority'].seed_server.material.ca_certificate)
            assert managed._current_member.__self__ is world['target']
            assert app.activate_runtime(managed)['state']=='running'
            native=session(sender,world)
            reply=native.send(command_id='app-owned-native-hello',command='hello',payload={})
            assert reply['ok'] is True
            world['target']._record_disposition('revoked')
            deadline=time.monotonic()+6
            while managed.status()['state']=='running' and time.monotonic()<deadline: time.sleep(.05)
            assert managed.status()['state']=='blocked'
            assert not managed.worker.process_group_present()
        finally:
            app.stop();old.close();sender.close()


def test_staging_identity_mismatch_fails_before_process(tmp_path):
    with _world(tmp_path/'world') as world:
        with pytest.raises(RuntimeErrorCode,match='runtime_staging_binding_mismatch'):
            runtime(tmp_path/'target',world,archive_digest='sha256:'+'0'*64)
