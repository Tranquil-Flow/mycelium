"""One owner's local contributor is a logical peer, not a second host."""
import pytest
from mycelium_a13_macos.app import DemoApp
from mycelium_a13_macos.local_contributor import create_local_contributor
from test_app_authority import CONSENT


def test_real_local_tls_pairing_staging_client_and_retained_restart(tmp_path):
    owner=DemoApp(tmp_path/'owner',check_platform=False).start();actor=None
    try:
        with pytest.raises(ValueError):create_local_contributor(owner)
        owner.action('/api/create',{'consent':CONSENT})
        seed=owner.authority.coordinator.signer.verification_key_digest
        actor=create_local_contributor(owner)
        key=actor.member.signer.verification_key_digest;node=actor.member.node_id
        assert node!=owner.authority.member.node_id
        assert actor.pending_stage() is None
        assert actor.status()['membership_state']=='current'
        assert actor.status()['route_ready'] is False
        assert not (actor.peer.state_root/'seed').exists()
        actor.stop();owner.action('/api/stop',{});owner.action('/api/start',{})
        actor=create_local_contributor(owner)
        assert actor.member.signer.verification_key_digest==key
        assert actor.member.node_id==node
        assert owner.authority.coordinator.signer.verification_key_digest==seed
        assert actor.pending_stage() is None
    finally:
        if actor:actor.stop()
        owner.close()


def test_bound_cache_rejects_escape_and_symlink(tmp_path):
    owner=DemoApp(tmp_path/'owner',check_platform=False).start();actor=None
    try:
        owner.action('/api/create',{'consent':CONSENT});actor=create_local_contributor(owner)
        cache=actor._preposition_cache_root({'preposition':{}})
        assert cache==owner.root/'model-source'
        parked=cache.with_name('model-source-parked');cache.rename(parked)
        cache.symlink_to(tmp_path,target_is_directory=True)
        try:
            with pytest.raises(ValueError):actor._preposition_cache_root({'preposition':{}})
        finally:cache.unlink();parked.rename(cache)
    finally:
        if actor:actor.stop()
        owner.close()


def test_app_owns_local_actor_and_routes_existing_staging_actions(tmp_path):
    owner=DemoApp(tmp_path/'owner',check_platform=False).start()
    try:
        owner.action('/api/create',{'consent':CONSENT})
        status=owner.action('/api/local-contributor/start',{})
        assert status['physical_hosts']==1
        assert owner.action('/api/staging/check',{})=={'offer':None}
        node=owner.local_contributor.member.node_id
        owner.action('/api/stop',{})
        assert owner.local_contributor is None
        owner.action('/api/start',{})
        assert owner.local_contributor.member.node_id==node
        assert owner.action('/api/control/stop',{})['state']=='stopped'
    finally:owner.close()
