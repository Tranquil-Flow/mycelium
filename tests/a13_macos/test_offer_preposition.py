# SPDX-License-Identifier: AGPL-3.0-only
"""Inert local artifact staging through the real signed HTTP path."""
import hashlib
from pathlib import Path
import pytest
from test_controller_managed_staging import setup_controller
from test_control_pump import _world
from test_managed_control import RUN_ID,DEPLOYMENT_ID
from mycelium_a13_macos.target_app import TargetApplication


def test_managed_staging_uses_only_target_owned_preposition_cache(tmp_path):
    from mycelium_a13_macos.controller_staging import ManagedControllerStaging
    from mycelium_a13_macos.authority import _private_directory
    with _world(tmp_path/'world') as w:
        box=w['authority']._control_mailbox
        app=TargetApplication(w['target'].state_root);app.peer=w['target']
        cache=_private_directory(app.peer.state_root/'serving',create=True)
        artifact=cache/'fixture-artifact';artifact.write_bytes(b'inert fixture')
        row={'destination_path':'deployment/artifact.bin','source_path':str(artifact),
             'size_bytes':len(artifact.read_bytes()),'content_digest':'sha256:'+hashlib.sha256(artifact.read_bytes()).hexdigest()}
        preposition={'protocol':'mycelium.controller_prepositioned_member_artifacts.v1','files':[row]}
        prepositions={'protocol':'mycelium.controller_prepositioned_artifacts.v1','members':{w['member']['node_id']:[row]}}
        adapter=ManagedControllerStaging(mailbox=box,members=[w['member']],run_id=RUN_ID,deployment_id=DEPLOYMENT_ID)
        controller,peer=setup_controller(tmp_path,w,staging_adapter=adapter,prepositioned_artifacts=prepositions)
        archive,digest=controller._archive_identity_for_peer(peer)
        offer=box.publish_offer(target=w['member'],run_id=RUN_ID,deployment_id=DEPLOYMENT_ID,
            archive=archive,preposition=preposition)
        ca=w['authority'].seed_server.material.ca_certificate
        ack=app.stage_offer(offer,max_bytes=len(archive),max_disk_bytes=100_000,
            authorize=lambda intent:True,_test_ca_certificate=ca)
        assert ack['preposition_digest']==offer['preposition_digest']
        assert (Path(peer.staging_root)/'deployment/artifact.bin').read_bytes()==b'inert fixture'
        assert controller._prepare_physical((),{})['actions'][0]['acknowledgement']==ack
        box.request_stage_cleanup(offer)
        app.cleanup_offer(offer,acknowledged=True,_test_ca_certificate=ca)
        assert not Path(peer.staging_root).exists()
        assert artifact.read_bytes()==b'inert fixture'
        assert controller._cleanup_peer(peer,archive_digest=digest,
            preposition_digest=offer['preposition_digest'])['removed'] is True


@pytest.mark.parametrize('case',['outside-cache','changed-digest','symlink-source','no-consent'])
def test_preposition_rejects_unsafe_source_or_missing_consent(tmp_path,case):
    from mycelium_a13_macos.authority import _private_directory
    from test_managed_staging import bundle
    with _world(tmp_path/'world') as w:
        app=TargetApplication(w['target'].state_root);app.peer=w['target']
        cache=_private_directory(app.peer.state_root/'serving',create=True)
        source=(tmp_path/'private-outside') if case=='outside-cache' else cache/'inert'
        source.write_bytes(b'fixture')
        if case=='symlink-source':
            linked=cache/'linked';linked.symlink_to(source);source=linked
        row={'destination_path':'deployment/inert','source_path':str(source),'size_bytes':7,
            'content_digest':'sha256:'+('0'*64 if case=='changed-digest' else hashlib.sha256(b'fixture').hexdigest())}
        archive,_=bundle()
        offer=w['authority']._control_mailbox.publish_offer(target=w['member'],run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID,archive=archive,
            preposition={'protocol':'mycelium.controller_prepositioned_member_artifacts.v1','files':[row]})
        with pytest.raises(ValueError):
            app.stage_offer(offer,max_bytes=len(archive),max_disk_bytes=100_000,
                authorize=lambda intent:case!='no-consent',
                _test_ca_certificate=w['authority'].seed_server.material.ca_certificate)
        assert source.read_bytes()==b'fixture'
        stage=app.peer.state_root/'managed'/RUN_ID/'mycelium-stage'
        # Failed canonical copy may leave a private quarantined stage, never a
        # positively acknowledged or configured artifact.
        from mycelium_a13_macos.managed_control import ManagedControlError
        with pytest.raises(ManagedControlError,match='offer_stage_unconfirmed'):
            w['authority']._control_mailbox.stage_receipt(offer)
        assert app._runtime is None

