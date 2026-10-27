# SPDX-License-Identifier: AGPL-3.0-only
"""Expired execution offers remain eligible only for target-proven cleanup."""
from pathlib import Path
import time

import pytest

from test_control_pump import _world
from test_controller_managed_staging import setup_controller
from test_managed_control import RUN_ID, DEPLOYMENT_ID
from mycelium_a13_macos.controller_staging import ManagedControllerStaging
from mycelium_a13_macos.managed_control import ManagedControlError
from mycelium_a13_macos.target_app import TargetApplication


@pytest.mark.parametrize('staged', [False, True])
def test_expired_offer_can_be_cleaned_but_never_staged(tmp_path, monkeypatch, staged):
    with _world(tmp_path / 'world') as w:
        box = w['authority']._control_mailbox
        adapter = ManagedControllerStaging(mailbox=box, members=[w['member']],
            run_id=RUN_ID, deployment_id=DEPLOYMENT_ID)
        controller, peer = setup_controller(tmp_path, w, staging_adapter=adapter)
        archive, digest = controller._archive_identity_for_peer(peer)
        offer = box.publish_offer(target=w['member'], run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID, archive=archive, ttl_seconds=30)
        app = TargetApplication(w['target'].state_root)
        app.peer = w['target']
        ca = w['authority'].seed_server.material.ca_certificate
        if staged:
            app.stage_offer(offer, max_bytes=len(archive), max_disk_bytes=100_000,
                authorize=lambda intent: True, _test_ca_certificate=ca)
        # Advance only this fixture's control clocks, not the host or membership.
        now = offer['expires_at'] + 1
        monkeypatch.setattr(box, '_clock', lambda: now)
        monkeypatch.setattr(w['target_client'], '_clock', lambda: now)
        monkeypatch.setattr(app.peer, 'staging_control_client', lambda **kw: w['target_client'])
        assert box.pending_offer(w['member']) is None
        with pytest.raises(ManagedControlError, match='offer_unavailable'):
            box.stage_receipt(offer)
        box.request_stage_cleanup(offer)
        assert app.pending_cleanup(_test_ca_certificate=ca) == offer
        ack = app.cleanup_offer(offer, acknowledged=True, _test_ca_certificate=ca)
        assert ack['removed'] is staged
        assert not Path(peer.staging_root).exists()
        assert controller._cleanup_peer(peer, archive_digest=digest) == ack
        assert app.pending_cleanup(_test_ca_certificate=ca) is None
        assert box.retire_offer(offer)['cleanup_ack'] == ack
        assert box.pending_offer(w['member']) is None


def test_requested_expired_cleanup_is_not_evicted_by_new_publication(tmp_path, monkeypatch):
    with _world(tmp_path / 'world') as w:
        box = w['authority']._control_mailbox
        offer = box.publish_offer(target=w['member'], run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID, archive=b'inert', ttl_seconds=30)
        box.request_stage_cleanup(offer)
        monkeypatch.setattr(box, '_clock', lambda: offer['expires_at'] + 1)
        with pytest.raises(ManagedControlError, match='offer_already_pending'):
            box.publish_offer(target=w['member'], run_id='next-run',
                deployment_id='next-deployment', archive=b'inert')
        assert box.cleanup_offer(w['member']) == offer


def test_stopped_publication_cleans_never_started_local_work_without_waiting(tmp_path, monkeypatch):
    import threading
    from physical_inference_qualification import ControllerError
    from mycelium_a13_macos import controller_staging
    with _world(tmp_path / 'world') as w:
        box = w['authority']._control_mailbox
        adapter = ManagedControllerStaging(mailbox=box, members=[w['member']],
            run_id=RUN_ID, deployment_id=DEPLOYMENT_ID)
        controller, peer = setup_controller(tmp_path, w, staging_adapter=adapter)
        archive, digest = controller._archive_identity_for_peer(peer)
        offer = box.publish_offer(target=w['member'], run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID, archive=archive)
        app = TargetApplication(w['target'].state_root)
        app.peer = w['target']
        adapter.stop_event = threading.Event()
        adapter.stop_event.set()
        receipts = []
        def cleanup_tick():
            requested = app.pending_cleanup(_test_ca_certificate=w['authority'].seed_server.material.ca_certificate)
            if requested:
                receipts.append(app.cleanup_offer(requested, acknowledged=True,
                    _test_ca_certificate=w['authority'].seed_server.material.ca_certificate))
        adapter.cleanup_progress = cleanup_tick
        monkeypatch.setattr(controller_staging.time, 'sleep',
                            lambda delay: pytest.fail('stopped publication kept waiting'))
        with pytest.raises(ControllerError, match='managed_workflow_stopped'):
            controller._prepare_physical((), {})
        assert len(receipts) == 1 and receipts[0]['removed'] is False
        assert box.cleanup_receipt(offer) == receipts[0]


def test_never_started_cleanup_has_real_signed_absence_receipt(tmp_path):
    with _world(tmp_path / 'world') as w:
        box = w['authority']._control_mailbox
        adapter = ManagedControllerStaging(mailbox=box, members=[w['member']],
            run_id=RUN_ID, deployment_id=DEPLOYMENT_ID)
        controller, peer = setup_controller(tmp_path, w, staging_adapter=adapter)
        archive, digest = controller._archive_identity_for_peer(peer)
        offer = box.publish_offer(target=w['member'], run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID, archive=archive)
        app = TargetApplication(w['target'].state_root)
        app.peer = w['target']
        box.request_stage_cleanup(offer)
        ack = app.cleanup_offer(offer, acknowledged=True,
            _test_ca_certificate=w['authority'].seed_server.material.ca_certificate)
        assert ack['removed'] is False and not Path(peer.staging_root).exists()
        assert controller._cleanup_peer(peer, archive_digest=digest) == ack
