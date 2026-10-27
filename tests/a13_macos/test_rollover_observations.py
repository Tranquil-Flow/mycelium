# SPDX-License-Identifier: AGPL-3.0-only
"""Reuse this run's verified bootstrap hello, not a previous run's authority."""
import copy
import json
import time

import pytest
from test_assignment_planning import enrolled, seed  # noqa: F401
from test_workflow_observations_shipping import _prepared_plan
from test_bootid_bounded_observation_wait import _bounded_wait
from mycelium_a13_macos import chain_observation as observation
from mycelium_a13_macos.session_factory import ManagedSessionFactory
from mycelium_a13_macos.workflow import owner_channel, prepare_serving_workflow


@pytest.fixture
def observed(tmp_path, enrolled, monkeypatch):
    authority, nodes, prepared, plan, roots, document = _prepared_plan(tmp_path, enrolled)
    run = document['controller']['run_plan']
    # This fixture's historic plan run ID is not a current timestamp. The
    # producer is injected (no native process); freshness is tested on capture.
    now = time.time()
    doc = {'protocol': 'mycelium.managed_hello_observations.v1',
           'run_id': run['run_id'], 'deployment_id': run['deployment_id'],
           'captured_at_unix_ms': int(now * 1000),
           'observations': [{k: p[k] for k in ('node_id', 'host_id', 'boot_id')}
                            for p in document['controller']['peers']]}
    channel = owner_channel(authority)
    members = tuple(authority.coordinator.member(n) for n in nodes)
    factory = ManagedSessionFactory(owner=channel, run_id=run['run_id'],
                                   deployment_id=run['deployment_id'], members=members)
    context = observation.ObservationContext(run['run_id'], run['deployment_id'], members,
                                           dict.fromkeys(nodes), {}, factory, authority.control_mailbox)
    monkeypatch.setattr(observation, 'collect_managed_hello_observations', lambda *a, **k: copy.deepcopy(doc))
    context.collect()
    def forbidden(*args, **kwargs):
        pytest.fail('workflow retried retired bootstrap hello')
    monkeypatch.setattr('mycelium_a13_macos.managed_observations.collect_managed_hello_observations', forbidden)
    clock = _bounded_wait(monkeypatch)
    try:
        yield authority, prepared, plan, roots, context, clock
    finally:
        channel.close()


def prepare(data):
    authority, prepared, plan, roots, context, clock = data
    return prepare_serving_workflow(authority=authority, channel=context.factory.owner,
        prepared=prepared, operator_plan=plan, target_source_roots=roots,
        observation_context=context)


def test_workflow_uses_fresh_verified_bootstrap_without_180_second_wait(observed):
    workflow = prepare(observed)
    context, clock = observed[-2:]
    bound = json.loads(workflow.launch.operator_plan.read_text())
    assert {p['node_id']: p['boot_id'] for p in bound['controller']['peers']} == {
        p['node_id']: p['boot_id'] for p in context.observations['observations']}
    assert clock == {'now': 0.0, 'sleeps': []}


@pytest.mark.parametrize('change', ['expired', 'future', 'generation', 'deployment', 'run', 'boot', 'host', 'missing', 'uncollected'])
def test_changed_or_stale_observation_fails_closed_without_retry(observed, monkeypatch, change):
    authority, prepared, plan, roots, context, clock = observed
    if change in ('expired', 'future'):
        captured = context.observations['captured_at_unix_ms'] / 1000
        monkeypatch.setattr(observation, '_reuse_clock', lambda: captured + (601 if change == 'expired' else -61))
    elif change == 'generation':
        original = context.factory.owner.current_member
        monkeypatch.setattr(context.factory.owner, 'current_member', lambda n: {**original(n), 'generation': 999})
    elif change == 'uncollected':
        context._verified_observations = None
    else:
        doc = json.loads(plan.read_text())
        if change in ('run', 'deployment'):
            doc['controller']['run_plan'][change + '_id'] += '-changed'
        elif change == 'missing':
            doc['controller']['peers'].pop()
        else:
            doc['controller']['peers'][0][change + '_id'] += 'changed'
        plan.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match='bootstrap_observation_reuse_refused'):
        prepare(observed)
    assert clock == {'now': 0.0, 'sleeps': []}
