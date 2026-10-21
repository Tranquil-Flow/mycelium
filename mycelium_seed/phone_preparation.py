# SPDX-License-Identifier: AGPL-3.0-only
"""Owner-local phone preparation using durable seed authority, never new keys.

Build receipts are approved out of band by their exact digest. This module does
not create receipts or establish that an APK was built from a source checkout.
The existing route-assignment and membership projections remain authoritative.
"""
from contextlib import contextmanager
import argparse
import json
from pathlib import Path
import sqlite3
import time
from urllib.parse import quote

from mycelium_membership import ASSIGNMENT_OFFER_PROTOCOL
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_seed.operator import _load_authority
from mycelium_seed import route_assignment as ra
from mycelium_seed.plan_binding import bind_operator_plan_document


def require(ok, code):
    if not ok:
        raise ra.SeedCoordinatorError(code)


@contextmanager
def authority(state_root, seed_key_digest, clock=None):
    # A callable is an injected trusted dependency, never an artifact field.
    clock = time.time if clock is None else clock
    lease, coordinator, binding = _load_authority(Path(state_root), incarnation="phone-preparation")
    connection = None
    try:
        require(binding['seed_key_digest'] == seed_key_digest, 'phone_authority_key_mismatch')
        ra._ensure_schema(lease.path/'state.sqlite3')
        connection = ra._connect(lease.path/'state.sqlite3')
        connection.execute('BEGIN IMMEDIATE')
        metadata = dict(connection.execute('SELECT key,value FROM seed_metadata'))
        require(metadata.get('seed_key_digest') == seed_key_digest, 'phone_authority_key_mismatch')
        members = {}
        for row in connection.execute('SELECT * FROM seed_members'):
            member = dict(row)
            member['runtime_capability'] = json.loads(member.pop('runtime_capability_json'))
            members[member['node_id']] = member
        now = clock()
        guards = []
        yield coordinator, connection, members, now, guards
        lease.revalidate()
        # Publication linearizes at this last eligibility check while the
        # write transaction still fences revocation/replacement. No output is
        # returned (or written by the CLI) if a verifier outlives eligibility.
        released_at = clock()
        require(released_at >= now, 'phone_authority_clock_invalid')
        for check in guards:
            check(released_at)
        connection.execute('COMMIT')
    finally:
        if connection is not None:
            if connection.in_transaction:
                connection.execute('ROLLBACK')
            connection.close()
        lease.close()


def current_member(members, node_id, now):
    member = members.get(node_id)
    require(member is not None, 'seed_member_unknown')
    require(member['lifecycle_state'] != 'STOPPED', 'seed_member_revoked')
    require(now < member['lease_expires_at'], 'seed_member_lease_expired')
    # Preparation is stricter than lease survival: an overdue heartbeat cannot
    # authorize a fresh load. No clock supplied by an artifact is trusted here.
    require(now <= member['next_heartbeat_due_at'], 'seed_member_liveness_stale')
    require(ra.peer_runtime_is_activation_eligible(member['peer_class'], member['runtime_capability']),
            'seed_member_activation_ineligible')
    return member


def _applied_current(row, member, now):
    """The row is the member's current applied assignment, under its current
    membership generation, endpoint and key. ``applied_generation`` is a
    MEMBERSHIP generation; it says who applied, never which load."""
    _applied_fresh(row, now)
    require(row['applied_generation'] == member['generation']
            and row['endpoint_id'] == member['endpoint_id']
            and row['verification_key_digest'] == member['verification_key_digest'],
            'phone_applied_assignment_required')


def _applied_fresh(row, now):
    require(row is not None and not row['withdrawn'] and row['issued_at'] <= now < row['expires_at'],
            'phone_applied_assignment_required')
    require(row['applied_at'] is not None and 0 <= now-row['applied_at'] <= 600,
            'phone_applied_assignment_required')


def _issue(coordinator, connection, members, *, document, node, run_id, deployment_id,
           controller_endpoint_id, approved_build_receipt_digest, now, guards):
    from scripts.qwen_phone_builder import PhoneEvidence, digest, BINDING_PROTOCOL, PREPARATION_PROVENANCE
    member = current_member(members, node['node_id'], now)
    require(member['peer_class'] == 'android_native_qwen2_iroh', 'seed_member_activation_ineligible')
    require(member['endpoint_id'] == node['endpoint_id'] and member['generation'] == node['membership_generation'],
            'phone_authority_identity_mismatch')
    row = connection.execute('SELECT * FROM seed_route_assignments WHERE node_id=?', (node['node_id'],)).fetchone()
    _applied_current(row, member, now)
    require(controller_endpoint_id in ra._controller_ids(row['controller_endpoint_ids_json']),
            'phone_controller_not_authorized')
    stored_manifest = json.loads(row['stage_pack_json'])
    require(stored_manifest == document['manifest'] and ra.v2_manifest.is_signed_stage_manifest(stored_manifest),
            'phone_applied_manifest_mismatch')
    require(digest(canonical_json_bytes(document['build_receipt'])) == approved_build_receipt_digest,
            'phone_build_receipt_not_approved')
    response = document['configured']['result']
    key = response['verification_key']
    require(key['verification_key_digest'] == member['verification_key_digest'], 'membership_key_pin_mismatch')
    details = response['observation']['details']
    require(details['assignment_id'] == row['assignment_id'], 'phone_applied_assignment_id_mismatch')
    def fresh(at):
        current_member(members, node['node_id'], at)
        _applied_fresh(row, at)
    guards.append(fresh)
    # load_generation is the phone's own signed count (PhoneEvidence below
    # refuses any record that merely echoes the controller). It is copied as
    # observed and compared with no seed, route or membership generation.
    profile = {k:details[k] for k in ('assignment_id','stage_id','manifest_digest','load_generation')}
    profile['pack_digest'] = details['phone_stage_pack_digest']
    prepared = connection.execute('SELECT * FROM seed_route_preparations WHERE node_id=?', (node['node_id'],)).fetchone()
    require(prepared is not None and prepared['assignment_id'] == row['assignment_id'], 'phone_preparation_binding_required')
    assignment = json.loads(prepared['assignment_json'])
    ra.validate_preparation_assignment(assignment, stored_manifest, node['node_id'])
    require(assignment['assignment_id'] == row['assignment_id']
            and assignment['deployment_id'] == deployment_id
            and assignment['manifest_digest'] == profile['manifest_digest'], 'phone_preparation_binding_mismatch')
    statement = dict(protocol=BINDING_PROTOCOL, node_id=node['node_id'], endpoint_id=member['endpoint_id'],
        incarnation=member['incarnation'], membership_generation=member['generation'], member_verification_key=key,
        run_id=run_id, deployment_id=deployment_id, issued_at=now,
        expires_at=min(now+600,member['lease_expires_at'],row['expires_at'],row['applied_at']+600),
        configure=profile, build_receipt_digest=approved_build_receipt_digest,
        controller_endpoint_id=controller_endpoint_id,
        preparation_binding=dict(assignment=assignment, route_generation=row['route_generation'],
            provenance=PREPARATION_PROVENANCE))
    envelope = dict(protocol=ra.SIGNED_MESSAGE_PROTOCOL, message=statement,
                    signature=coordinator.signer.sign(statement), verification_key=coordinator.signer.public_key_record())
    # Verify original phone signatures/identity/build/golden before returning any
    # seed authorization. The provisional envelope never leaves on refusal.
    PhoneEvidence(dict(document, authorization=envelope), seed_key_digest=coordinator.signer.verification_key_digest,
                  node=node, run_id=run_id, deployment_id=deployment_id, now=now)
    guards.append(lambda at: require(at < statement['expires_at'], 'phone_authority_stale'))
    return envelope


def issue_builder_authorization(state_root, *, seed_key_digest, document, node, run_id,
                                deployment_id, controller_endpoint_id, approved_build_receipt_digest, clock=None):
    with authority(state_root, seed_key_digest, clock) as (coordinator, connection, members, now, guards):
        return _issue(coordinator, connection, members, document=document, node=node, run_id=run_id,
            deployment_id=deployment_id, controller_endpoint_id=controller_endpoint_id,
            approved_build_receipt_digest=approved_build_receipt_digest, now=now, guards=guards)


def preparation_membership_snapshot(state_root, *, seed_key_digest, assignments, packs, graph_document,
                                    topology, phone_evidence, controller_endpoint_id, clock=None, placement_provenance="frozen_fixture"):
    """Project current seed offers AFTER the exact graph exists; no circular input.

    Revalidate each seed-pinned load against current durable authority. Reuse the
    existing plan binder rather than creating a second offer protocol or signer.
    """
    from scripts.qwen_phone_builder import digest
    by_node = {n['node_id']:n for n in topology}
    with authority(state_root, seed_key_digest, clock) as (coordinator, connection, members, now, guards):
        selected = [current_member(members, a['node_id'], now) for a in assignments]
        for member in selected:
            guards.append(lambda at, node_id=member['node_id']: current_member(members, node_id, at))
        for member in selected:
            node = by_node[member['node_id']]
            require(member['endpoint_id'] == node['endpoint_id'] and member['generation'] == node['membership_generation'],
                    'phone_membership_assignment_mismatch')
        expires = min(now+600, *(m['lease_expires_at'] for m in selected))
        for node_id,e in phone_evidence.items():
            require(e.statement['controller_endpoint_id'] == controller_endpoint_id, 'phone_controller_not_authorized')
            auth = _issue(coordinator, connection, members, document=e.document, node=by_node[node_id],
                run_id=e.statement['run_id'], deployment_id=e.statement['deployment_id'],
                controller_endpoint_id=controller_endpoint_id,
                approved_build_receipt_digest=e.statement['build_receipt_digest'], now=now, guards=guards)
            guards.append(lambda at, expires=e.statement['expires_at']: require(at < expires, 'phone_authority_stale'))
            expires = min(expires, e.statement['expires_at'], auth['message']['expires_at'])
        offers = []
        for a,pack in zip(assignments,packs,strict=True):
            e = phone_evidence.get(a['node_id'])
            message = dict(protocol=ASSIGNMENT_OFFER_PROTOCOL, deployment_id=a['deployment_id'],
                deployment_epoch=a['deployment_epoch'], assignment_id=a['assignment_id'],
                recipient_node_id=a['node_id'], assignment_digest=digest(canonical_json_bytes(a)),
                stage_pack_digest=e.profile['pack_digest'] if e else pack['stage_pack_digest'],
                graph_digest=digest(canonical_json_bytes(graph_document)),
                load_generation=e.native_load_generation if e else 1,
                placement_provenance=placement_provenance,
                peer_endpoint_records=[dict(node_id=m['node_id'],endpoint_id=m['endpoint_id'])
                                       for m in selected if m['node_id'] != a['node_id']])
            offers.append({'message':message})
        plan = {'controller':{'membership_snapshot':dict(protocol='mycelium.controller_membership_snapshot.v1',
                     seed_key_digest=seed_key_digest,swarm_id=coordinator.swarm_id,assignment_offers=offers,
                     deployment_id=assignments[0]["deployment_id"],deployment_epoch=assignments[0]["deployment_epoch"])}}
        # Bound freshness through the existing projection's member lease cap.
        projected = [dict(m,lease_expires_at=min(m['lease_expires_at'],expires)) for m in selected]
        return bind_operator_plan_document(plan,signer=coordinator.signer,swarm_id=coordinator.swarm_id,
                   seed_node_id=coordinator.seed_node_id,members=projected,now=now)['controller']['membership_snapshot']


_PROOF_FIELDS = frozenset({
    'protocol', 'assignment_digest', 'node_id', 'runtime_backend', 'runtime', 'phone_configure',
    'manifest', 'authorization', 'configured', 'snapshot', 'build_receipt', 'route_ready',
    'load_identity_provenance', 'preparation_binding', 'source_binding', 'claim_boundary'})


def _read_current_authority(database, node_id):
    """One read transaction: seed key, member, applied route and preparation rows.

    Separate reads could pair a member from before a rotation with a route row
    from after it. Nothing is written and no lock outlives this call.
    """
    uri = f"file:{quote(str(database), safe='/')}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5.0, isolation_level=None)
    except sqlite3.Error as exc:
        raise ra.SeedCoordinatorError('phone_load_authority_unavailable') from exc
    try:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only = ON')
        connection.execute('BEGIN')
        metadata = dict(connection.execute("SELECT key,value FROM seed_metadata WHERE key='seed_key_digest'"))
        member = connection.execute('SELECT * FROM seed_members WHERE node_id=?', (node_id,)).fetchone()
        row = connection.execute('SELECT * FROM seed_route_assignments WHERE node_id=?', (node_id,)).fetchone()
        prepared = connection.execute('SELECT * FROM seed_route_preparations WHERE node_id=?', (node_id,)).fetchone()
        connection.execute('COMMIT')
        return (metadata.get('seed_key_digest'), None if member is None else dict(member),
                None if row is None else dict(row), None if prepared is None else dict(prepared))
    except sqlite3.Error as exc:
        raise ra.SeedCoordinatorError('phone_load_authority_unavailable') from exc
    finally:
        connection.close()


def current_load_authority(*, database, seed_key_digest, revalidate, node_id, endpoint_id, incarnation,
                           membership_generation, staging_root, run_id, deployment_id, configure,
                           placement_assignment_id, placement_load_proof_digest, graph_manifest_digest,
                           proof, assignment, model_manifest, clock=None):
    """Validate one phone load against CURRENT seed authority and the packaged proof.

    This is the final-qualification and renewal counterpart of ``_issue``. The
    600 second window of the seed authorization was a preparation-time check
    and is not repeated; its signature and content are verified under the
    pinned seed key, and freshness comes from the current member and applied
    route rows read here. The caller compares the returned identity with the
    natively signed observations of its own route open.

    Nothing is inferred: every equality below is between two independently
    held values. A refusal raises; there is no partial or advisory result.
    """
    from scripts.qwen_phone_builder import PhoneEvidence, digest, PROOF_PROTOCOL
    from mycelium_qualification.phone import (
        LOAD_IDENTITY_PROVENANCE_V2, _seal_phone_load_authority, phone_load_proof_digest)
    clock = time.time if clock is None else clock
    revalidate()
    pinned, member, row, prepared = _read_current_authority(database, node_id)
    now = clock()
    require(pinned == seed_key_digest, 'phone_authority_key_mismatch')
    require(member is not None, 'seed_member_unknown')
    # The route separately requires RUNNING to open a session; here a member
    # that is leaving or gone can never back a load.
    require(member['lifecycle_state'] not in ('STOPPING', 'STOPPED'), 'seed_member_revoked')
    require(now < member['lease_expires_at'], 'seed_member_lease_expired')
    require(member['peer_class'] == 'android_native_qwen2_iroh'
            and ra.peer_runtime_is_activation_eligible(member['peer_class'], json.loads(member['runtime_capability_json'])),
            'seed_member_activation_ineligible')
    require(member['endpoint_id'] == endpoint_id and member['incarnation'] == incarnation
            and member['generation'] == membership_generation, 'phone_authority_identity_mismatch')
    _applied_current(row, member, now)
    require(isinstance(proof, dict) and set(proof) == _PROOF_FIELDS and proof['protocol'] == PROOF_PROTOCOL
            and proof['route_ready'] is False and proof['node_id'] == node_id
            and proof['load_identity_provenance'] == LOAD_IDENTITY_PROVENANCE_V2, 'phone_load_proof_invalid')
    require(row['assignment_id'] == configure.get('assignment_id'), 'phone_applied_assignment_id_mismatch')
    node = dict(node_id=node_id, endpoint_id=member['endpoint_id'],
                membership_generation=member['generation'], staging_root=staging_root)
    evidence = PhoneEvidence({k: proof[k] for k in ('authorization', 'manifest', 'build_receipt', 'configured', 'snapshot')},
        seed_key_digest=seed_key_digest, node=node, run_id=run_id, deployment_id=deployment_id,
        now=now, preparation_window=False)
    statement = evidence.statement
    require(statement['incarnation'] == member['incarnation'], 'phone_authority_identity_mismatch')
    require(statement['member_verification_key'].get('verification_key_digest') == member['verification_key_digest'],
            'membership_key_pin_mismatch')
    require(statement['controller_endpoint_id'] in ra._controller_ids(row['controller_endpoint_ids_json']),
            'phone_controller_not_authorized')
    # A row re-issued after the authorization is another authority instance,
    # even when it names the same assignment and the phone never reloaded.
    require(row['issued_at'] <= statement['issued_at'], 'phone_applied_assignment_reissued')
    stored_manifest = json.loads(row['stage_pack_json'])
    require(stored_manifest == proof['manifest'] and ra.v2_manifest.is_signed_stage_manifest(stored_manifest)
            and row['stage_pack_digest'] == evidence.manifest['pack_digest'] == evidence.profile['pack_digest'],
            'phone_applied_manifest_mismatch')
    require(prepared is not None and prepared['assignment_id'] == row['assignment_id'],
            'phone_preparation_binding_required')
    compiled = json.loads(prepared['assignment_json'])
    ra.validate_preparation_assignment(compiled, stored_manifest, node_id)
    binding = statement['preparation_binding']
    require(compiled == binding['assignment'] == assignment == proof['preparation_binding']['assignment']
            and binding == proof['preparation_binding']
            and binding['route_generation'] == row['route_generation']
            and compiled['assignment_id'] == row['assignment_id'] == evidence.profile['assignment_id']
            and compiled['deployment_id'] == deployment_id
            and proof['assignment_digest'] == digest(canonical_json_bytes(compiled)),
            'phone_preparation_binding_mismatch')
    require(dict(configure) == evidence.profile == proof['phone_configure'], 'phone_configure_mismatch')
    require(proof['source_binding'] == evidence.bind_packaged(compiled, model_manifest=model_manifest),
            'phone_packaged_source_mismatch')
    observed = evidence.observations[0]
    load_digest = phone_load_proof_digest(observed)
    require(placement_assignment_id == row['assignment_id']
            and graph_manifest_digest == compiled['manifest_digest'] == evidence.profile['manifest_digest']
            and placement_load_proof_digest == load_digest, 'phone_graph_placement_mismatch')
    revalidate()
    details = observed['details']
    return _seal_phone_load_authority(
        node_id=node_id, endpoint_id=member['endpoint_id'], process_id=member['incarnation'],
        member_key_digest=member['verification_key_digest'], membership_generation=member['generation'],
        seed_key_digest=seed_key_digest, assignment_id=row['assignment_id'],
        assignment_digest=proof['assignment_digest'], stage_id=evidence.profile['stage_id'],
        pack_digest=row['stage_pack_digest'], manifest_digest=compiled['manifest_digest'],
        load_generation=details['load_generation'], runtime_backend=details['runtime_backend'],
        runtime_build_digest=details['runtime_build_digest'],
        golden_output_digest=details['golden_output_digest'], load_proof_digest=load_digest, checked_at=now)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state-root',type=Path,required=True)
    p.add_argument('--seed-key-digest',required=True)
    p.add_argument('--request',type=Path,required=True,help='document, node, run_id, deployment_id, controller_endpoint_id')
    p.add_argument('--approved-build-receipt-digest',required=True,help='Trusted build authority approval, NOT inferred from request')
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args(argv)
    request=json.loads(a.request.read_text())
    require(isinstance(request, dict) and set(request) == {
        'document', 'node', 'run_id', 'deployment_id', 'controller_endpoint_id'}, 'phone_preparation_request_invalid')
    result=issue_builder_authorization(a.state_root,seed_key_digest=a.seed_key_digest,
        approved_build_receipt_digest=a.approved_build_receipt_digest,**request)
    from mycelium_seed.operator import _write_private
    _write_private(a.output,canonical_json_bytes(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
