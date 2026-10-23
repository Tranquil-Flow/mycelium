# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit ON authority for scripted desk routes, never device evidence.

These doubles replace only host provisioning. Grants, receipt signatures,
receipt ordering and owner delivery use the real implementation. An observer
owner refuses inference: it cannot silently fall back to the per-token path.
"""
from types import SimpleNamespace
import time

from mycelium_live.token_path_owner import OwnerTokenPath
from mycelium_qualification.signing import generate_ed25519_signer
from mycelium_router.token_path import TokenBatch
from mycelium_router.token_path_authority import ReceiptJournal
from tests.live.test_token_path_owner import Mailbox


class ObserverTokenOwner:
    enabled = True
    required = True

    def attach_route(self, route):
        self.route = route

    def prepare(self, **kwargs) -> object:
        raise AssertionError('observer-only fixture must not execute inference')


class ScriptedTokenOwner(ObserverTokenOwner):
    def __init__(self):
        self.signer = generate_ed25519_signer(endpoint_id='scripted-owner')
        self.entries = {}
        self.attempts = {}
        self.sealed = []

    def prepare(self, **kwargs):
        nodes = sorted(kwargs['participating_node_ids'])
        manifest = kwargs['manifest']
        placements = {p.placement_id: p.node_id for s in self.route._graph.stages for p in s.placements}
        from mycelium_router.serialization import path_manifest_from_dict
        entry = placements[path_manifest_from_dict(manifest).ordered_hops[0].placement_id]
        for node in nodes:
            self.entries.setdefault(node, generate_ed25519_signer(endpoint_id='scripted-'+node))
        signer = self.entries[entry]
        pins = dict(run_id=self.route._plan['run_id'], requester_id='scripted-requester',
            owner_endpoint_id=self.signer.endpoint_id, owner_key_digest=self.signer.verification_key_digest,
            entry_node_id=entry, entry_endpoint_id=signer.endpoint_id, entry_key_digest=signer.verification_key_digest,
            participants=[dict(node_id=n, endpoint_id=self.entries[n].endpoint_id, generation=1) for n in nodes],
            model_digest=getattr(self.route._graph, 'manifest_digest', 'sha256:'+'c'*64))
        box = Mailbox()
        deadline = time.monotonic()+60
        owner = OwnerTokenPath(enabled=True, signer=self.signer, scope_pins=pins,
            entry_key=signer.public_key_record(), mailbox=box, seal=self.sealed.append,
            current=lambda: not self.route._closed, capable=lambda n: n in nodes,
            max_new_tokens=65536, stop_token_ids=kwargs['stop_token_ids'],
            process_deadline_monotonic_s=deadline, lease_deadline_monotonic_s=deadline,
            clock_uncertainty_ms=100)
        owner.required = True
        attempt = owner.prepare(**kwargs)
        assert attempt is not None, 'ON fixtures must issue a real grant, never fall back'
        self.attempts[kwargs['command_id']] = SimpleNamespace(
            attempt=attempt, box=box, journal=ReceiptJournal(grant=attempt.grant, signer=signer))
        return attempt

    def publish(self, command_id, *, tokens, status):
        state = self.attempts[command_id]
        receipt = state.journal.append(TokenBatch(0, 0, tuple(tokens), status))
        state.box.stream.accept(receipt)
        return dict(status=status, token_count=len(tokens), token_grant_digest=state.attempt.grant.digest)
