# SPDX-License-Identifier: AGPL-3.0-only
"""Canonical controller staging over target-owned signed mailbox receipts.

Only application-selected, current member/run offers are consumed. No SSH,
remote argv, fabricated native output or route qualification is introduced.
The caller publishes offers before starting the controller; the target accepts
staging/cleanup through its independently consented application boundary.
"""
from __future__ import annotations
import hashlib
import json
import math
import time
from physical_inference_qualification import CommandCapture, ControllerError, _canonical_bytes
from .managed_control import ManagedControlError, _binding_from_member, _BINDING_FIELDS
from .managed_offers import ManagedProvisioningMailbox


class ManagedControllerStaging:
    def __init__(self,*,mailbox,members,run_id,deployment_id):
        if not isinstance(mailbox,ManagedProvisioningMailbox):
            raise ControllerError('managed_staging_mailbox_invalid')
        self.mailbox=mailbox
        self.run_id=run_id;self.deployment_id=deployment_id
        self.members={}
        for member in members:
            copy=json.loads(json.dumps(dict(member),allow_nan=False))
            _binding_from_member(copy,run_id,deployment_id)
            if copy['node_id'] in self.members:raise ControllerError('managed_staging_members_invalid')
            self.members[copy['node_id']]=copy
        if not self.members:raise ControllerError('managed_staging_members_invalid')

    def _offer(self,peer,archive_digest,preposition_digest):
        if peer.process_transport!='managed_https' or peer.node_id not in self.members:
            raise ControllerError('managed_staging_member_invalid')
        try:
            member=self.members[peer.node_id]
            offer=self.mailbox.pending_offer(member)
            binding=_binding_from_member(member,self.run_id,self.deployment_id)
            if offer is None or any(offer[k]!=binding[k] for k in _BINDING_FIELDS) or (offer['archive_digest']!=archive_digest or offer.get('preposition_digest')!=preposition_digest):
                raise ControllerError('managed_staging_offer_mismatch')
            return offer
        except ManagedControlError as exc:
            raise ControllerError('managed_staging_member_not_current') from exc

    def _wait(self,offer,*,cleanup,timeout_seconds):
        if type(timeout_seconds) not in (int,float) or not math.isfinite(timeout_seconds) or timeout_seconds<=0:
            raise ControllerError('managed_staging_timeout_invalid')
        deadline=time.monotonic()+min(timeout_seconds,300)
        reader=self.mailbox.cleanup_receipt if cleanup else self.mailbox.stage_receipt
        pending='offer_cleanup_unconfirmed' if cleanup else 'offer_stage_unconfirmed'
        while True:
            try:return reader(offer)
            except ManagedControlError as exc:
                if str(exc)!=pending:raise ControllerError('managed_staging_offer_unavailable') from exc
            remaining=deadline-time.monotonic()
            if remaining<=0:raise ControllerError('managed_staging_ack_timeout')
            time.sleep(min(0.05,remaining))

    @staticmethod
    def _capture(peer,ack):
        if ack['staging_root']!=peer.staging_root:
            raise ControllerError('managed_staging_root_mismatch')
        # Canonical controller's existing ack parser remains the final consumer.
        return CommandCapture(argv=(),returncode=0,stdout=_canonical_bytes(ack),stderr=b'')

    def prepare(self,*,peer,archive,archive_digest,preposition_bytes,preposition_digest,timeout_seconds):
        if (not isinstance(archive,bytes) or 'sha256:'+hashlib.sha256(archive).hexdigest()!=archive_digest
            or not isinstance(preposition_bytes,bytes) or
            (None if preposition_bytes==b'' else 'sha256:'+hashlib.sha256(preposition_bytes).hexdigest())!=preposition_digest):
            raise ControllerError('managed_staging_archive_invalid')
        offer=self._offer(peer,archive_digest,preposition_digest)
        if offer['archive_bytes']!=len(archive):raise ControllerError('managed_staging_archive_invalid')
        return self._capture(peer,self._wait(offer,cleanup=False,timeout_seconds=timeout_seconds))

    def cleanup(self,*,peer,archive_digest,preposition_digest,timeout_seconds):
        offer=self._offer(peer,archive_digest,preposition_digest)
        self.mailbox.request_stage_cleanup(offer)
        return self._capture(peer,self._wait(offer,cleanup=True,timeout_seconds=timeout_seconds))
