# SPDX-License-Identifier: AGPL-3.0-only
"""Behavioural proof that the plan binding receives LIVE boot-id observations.

WHY THIS FILE EXISTS
    tests/a13_operator/test_workflow_bootid_ordering.py pins the ORDER of two source lines by string
    index. It was GREEN on 2026-09-19 while prepare_serving_workflow could not collect a single
    observation: the import of collect_managed_hello_observations sat BELOW the try that used it, so
    the name was function-local, every call raised UnboundLocalError, and the `except Exception`
    reported it as `boot_id_observation_unavailable` -- the documented inert default. That is why the
    app suite stayed green and why no round log ever printed `boot_id_observations_collected`
    (measured in runsoak04-owner-daemon.log on the deployed preview.72 bundle).

    A source-text assertion cannot see that class of defect, so these tests drive the real function
    and assert on the plan that is actually bound. The failure is then observable: either the
    observations reach the binding or they do not.

    The collector is replaced at the module boundary, which managed_observations.py explicitly blesses
    ("test fakes must be injected by monkeypatching the session boundary, never by passing an
    arbitrary factory into this API").
"""
import hashlib
import json
import shutil
from pathlib import Path

import pytest
from test_assignment_planning import enrolled, seed  # noqa: F401 -- `seed` is the fixture `enrolled` needs
from test_serving import _native_snapshot, _operator_plan
from mycelium_a13_macos.managed_observations import ManagedObservationsError
from mycelium_a13_macos.serving import prepare_local_source
from mycelium_a13_macos.workflow import owner_channel, prepare_serving_workflow


def _prepared_plan(tmp_path, enrolled):
    """Assemble a native-schema plan over the enrolled members.

    Copied deliberately from tests/a13_macos/test_serving_workflow.py
    (test_compiled_plan_binds_real_members_and_inert_serving_composition), which is the proven
    assembly for this seam; it is not shared to avoid editing a passing test.
    """
    authority, peer, nodes, _, _ = enrolled
    prepared = prepare_local_source(_native_snapshot(tmp_path / 'source'))
    plan = _operator_plan(tmp_path / 'native', prepared, seed=authority)
    document = json.loads(plan.read_text())
    mapping = {'a13-local-0': nodes[0], 'a13-local-1': nodes[1], 'node-a': nodes[0], 'node-b': nodes[1]}

    def rename(value):
        if isinstance(value, str):
            return mapping.get(value, value)
        if isinstance(value, list):
            return [rename(x) for x in value]
        if isinstance(value, dict):
            return {k: rename(v) for k, v in value.items()}
        return value

    document = rename(document)
    controller = document['controller']
    from tests.seed_coordinator.test_plan_binding import _two_member_plan
    offers = rename(_two_member_plan())['controller']['membership_snapshot']['assignment_offers']
    for envelope in offers:
        for endpoint in envelope['message']['peer_endpoint_records']:
            endpoint['endpoint_id'] = authority.coordinator.member(endpoint['node_id'])['endpoint_id']
    controller['membership_snapshot']['assignment_offers'] = offers
    for row in controller['peers']:
        row.update(process_transport='managed_https', ssh_target=None, ssh_identity_file=None)
    bundle = Path(controller['source_root'])
    for path in (bundle / 'control').glob('*.json'):
        path.write_text(json.dumps(rename(json.loads(path.read_text()))))
    (bundle / 'physical_inference_node.py').write_text('# inert transfer fixture; installed entrypoint is used\n')
    (bundle / 'deployment').mkdir()
    shutil.copyfile(prepared.source_root / 'model.safetensors', bundle / 'deployment/model.safetensors')
    records = []
    for path in sorted(p for p in bundle.rglob('*') if p.is_file()):
        raw = path.read_bytes()
        records.append({'path': str(path.relative_to(bundle)), 'size_bytes': len(raw),
                        'content_digest': 'sha256:' + hashlib.sha256(raw).hexdigest()})
    controller['transfer_manifest'] = {'protocol': 'mycelium.controller_transfer_manifest.v1', 'files': records}
    controller['node_transfer_manifests'] = {'protocol': 'mycelium.controller_node_transfer_manifests.v1',
                                             'manifests': {node: controller['transfer_manifest'] for node in nodes}}
    plan.write_text(json.dumps(document))
    roots = {node: str(prepared.source_root) for node in nodes}
    return authority, nodes, prepared, plan, roots, document


def _fake_document(run_id, deployment_id, node_ids):
    """A shape-valid v1 observations document; distinct identities per node."""
    return {'protocol': 'mycelium.managed_hello_observations.v1',
            'deployment_id': deployment_id,
            'run_id': run_id,
            'captured_at_unix_ms': 1_789_800_000_000,
            'observations': [{'node_id': node,
                              'host_id': 'host-' + hashlib.sha256(('h' + node).encode()).hexdigest()[:32],
                              'boot_id': 'boot-' + hashlib.sha256(('b' + node).encode()).hexdigest()[:32]}
                             for node in node_ids]}


def test_observations_reach_the_bound_plan_when_a_hello_is_available(tmp_path, enrolled, monkeypatch):
    """The whole point of the caller change: a live observation must reach the binding.

    Fails while the import sits below its use, because the call raises UnboundLocalError before the
    collector can be reached at all -- the fake below is never entered and the bound peers keep the
    boot ids the topology recorded.
    """
    authority, nodes, prepared, plan, roots, document = _prepared_plan(tmp_path, enrolled)
    peer_nodes = [peer['node_id'] for peer in document['controller']['peers']]
    calls = []

    def fake_collect(factory, *, run_id, deployment_id, node_ids):
        calls.append({'run_id': run_id, 'deployment_id': deployment_id, 'node_ids': list(node_ids)})
        return _fake_document(run_id, deployment_id, node_ids)

    monkeypatch.setattr('mycelium_a13_macos.managed_observations.collect_managed_hello_observations',
                        fake_collect)
    channel = owner_channel(authority)
    try:
        workflow = prepare_serving_workflow(authority=authority, channel=channel, prepared=prepared,
                                            operator_plan=plan, target_source_roots=roots)
        assert len(calls) == 1, 'the collector was never called: %r' % (calls,)
        assert set(calls[0]['node_ids']) == set(peer_nodes)
        assert calls[0]['run_id'] == document['controller']['run_plan']['run_id']
        assert calls[0]['deployment_id'] == document['controller']['run_plan']['deployment_id']
        bound = json.loads(workflow.launch.operator_plan.read_text())
        expected = {item['node_id']: item['boot_id']
                    for item in _fake_document(calls[0]['run_id'], calls[0]['deployment_id'], peer_nodes)['observations']}
        actual = {peer['node_id']: peer.get('boot_id') for peer in bound['controller']['peers']}
        assert actual == expected, 'the observations did not reach the bound plan: %r' % (actual,)
        # The plan's controller field set is CLOSED. Storing the refresh inside the plan made
        # load_operator_plan refuse the whole document (plan_unknown_field), which stops serving -- so
        # the refresh must be audited OUTSIDE the plan and the plan must stay schema-valid.
        assert 'boot_id_bindings' not in bound['controller'], \
            'an extra controller field refuses the plan: plan_unknown_field'
        from mycelium_physical_runner.config import load_operator_plan
        load_operator_plan(workflow.launch.operator_plan)  # the native schema must still accept it
    finally:
        channel.close()


def test_a_scope_error_in_collection_is_not_reported_as_unavailability(tmp_path, enrolled, monkeypatch):
    """A programming error must not masquerade as 'the nodes are not talking yet'.

    The observed failure of 2026-09-19 was exactly this exception, swallowed and reported as
    boot_id_observation_unavailable, which made a dead code path look like an expected condition.
    """
    authority, nodes, prepared, plan, roots, document = _prepared_plan(tmp_path, enrolled)

    def broken_collect(factory, *, run_id, deployment_id, node_ids):
        raise UnboundLocalError("cannot access local variable 'collect_managed_hello_observations'"
                                " where it is not associated with a value")

    monkeypatch.setattr('mycelium_a13_macos.managed_observations.collect_managed_hello_observations',
                        broken_collect)
    channel = owner_channel(authority)
    try:
        with pytest.raises(UnboundLocalError):
            prepare_serving_workflow(authority=authority, channel=channel, prepared=prepared,
                                     operator_plan=plan, target_source_roots=roots)
    finally:
        channel.close()


def test_an_expected_unavailability_keeps_the_binding_inert(tmp_path, enrolled, monkeypatch):
    """The property that makes the change safe to ship: no observations means no change.

    A ManagedObservationsError is the producer's own refusal (no session, stale run id, malformed
    hello). That one IS expected, and must still leave the peers exactly as the topology recorded.
    """
    authority, nodes, prepared, plan, roots, document = _prepared_plan(tmp_path, enrolled)
    before = {peer['node_id']: peer.get('boot_id') for peer in document['controller']['peers']}
    # Timing knob only: the retry loop added for p80r1 (workflow.py, bounded budget + backoff) is pinned
    # on its own in tests/a13_macos/test_bootid_bounded_observation_wait.py with a deterministic clock.
    # Here the budget is zeroed so the inert outcome is reached in ONE attempt with no waiting at all.
    monkeypatch.setattr('mycelium_a13_macos.workflow._BOOT_ID_OBSERVATION_BUDGET_SECONDS', 0.0)

    def unavailable_collect(factory, *, run_id, deployment_id, node_ids):
        raise ManagedObservationsError('managed_observation_session_failed')

    monkeypatch.setattr('mycelium_a13_macos.managed_observations.collect_managed_hello_observations',
                        unavailable_collect)
    channel = owner_channel(authority)
    try:
        workflow = prepare_serving_workflow(authority=authority, channel=channel, prepared=prepared,
                                            operator_plan=plan, target_source_roots=roots)
        bound = json.loads(workflow.launch.operator_plan.read_text())
        after = {peer['node_id']: peer.get('boot_id') for peer in bound['controller']['peers']}
        assert after == before, 'an unavailable observation changed the bound peers: %r' % (after,)
        assert 'boot_id_bindings' not in bound['controller']
    finally:
        channel.close()
