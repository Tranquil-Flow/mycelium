# SPDX-License-Identifier: AGPL-3.0-only
"""A finished staging exchange can release its slot without time-based safety."""
import pytest
from test_control_pump import _world
from test_managed_control import RUN_ID, DEPLOYMENT_ID
from test_managed_staging import bundle
from mycelium_a13_macos.target_app import TargetApplication
from mycelium_a13_macos.managed_control import ManagedControlError


def test_retirement_requires_target_cleanup_and_retains_receipt(tmp_path):
    with _world(tmp_path/'world') as w:
        box=w['authority']._control_mailbox
        app=TargetApplication(w['target'].state_root);app.peer=w['target']
        archive,_=bundle()
        offer=box.publish_offer(target=w['member'],run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID,archive=archive)
        ca=w['authority'].seed_server.material.ca_certificate
        with pytest.raises(ManagedControlError,match='offer_cleanup_unconfirmed'):
            box.retire_offer(offer)
        app.stage_offer(offer,max_bytes=len(archive),max_disk_bytes=100_000,
            authorize=lambda intent:True,_test_ca_certificate=ca)
        stage=box.stage_receipt(offer)
        box.request_stage_cleanup(offer)
        with pytest.raises(ManagedControlError,match='offer_cleanup_unconfirmed'):
            box.retire_offer(offer)
        cleanup=app.cleanup_offer(offer,acknowledged=True,_test_ca_certificate=ca)
        receipt=box.retire_offer(offer)
        assert receipt=={'offer':offer,'stage_ack':stage,'cleanup_ack':cleanup}
        assert box.retire_offer(offer)==receipt
        assert box.pending_offer(w['member']) is None
        # New work is admitted immediately, not after an arbitrary expiry sleep.
        next_offer=box.publish_offer(target=w['member'],run_id=RUN_ID+'-next',
            deployment_id=DEPLOYMENT_ID,archive=archive)
        assert next_offer['offer_id']!=offer['offer_id']
        assert box.retire_offer(offer)==receipt
        assert box.pending_offer(w['member'])==next_offer
        from mycelium_a13_macos.managed_offers import ManagedProvisioningMailbox
        reopened=ManagedProvisioningMailbox(box.database,coordinator=w['authority'].coordinator)
        try:assert reopened.retire_offer(offer)==receipt
        finally:reopened.close()
        altered={**offer,'archive_digest':'sha256:'+'0'*64}
        with pytest.raises(ManagedControlError):box.retire_offer(altered)
        assert box.pending_offer(w['member'])==next_offer
