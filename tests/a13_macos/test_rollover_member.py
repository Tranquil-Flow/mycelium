# SPDX-License-Identifier: AGPL-3.0-only
"""Joined members reuse only bound, digest-verified bytes, never consent."""
from pathlib import Path

import pytest
from test_demo_target import world, announcement, node_plan


def start_slice(app, agent, state, plan, key='a' * 64, run='first'):
    state['a'] = announcement('collect', run_id=run)
    agent.tick()
    state['a'] = announcement('work', run_id=run, node_slice_plan=plan, artifact_reuse_key=key)
    agent.tick()
    agent.join_slice(5)
    assert agent.tick()['state'] == 'slice_ready'
    return agent.slice_root(run)


def setup_binding(app):
    identity = {'node_id': 'node-b', 'generation': 1, 'incarnation': 'member-1',
                'verification_key_digest': 'key-1', 'endpoint_id': 'endpoint-1'}
    app.authority.peer.staging_control_snapshot = lambda: dict(identity)
    return identity


def test_next_run_reuses_slice_bytes_and_still_revalidates(world, monkeypatch):
    app, agent, gate, state, calls = world
    setup_binding(app)
    plan = node_plan()
    first = start_slice(app, agent, state, plan)
    file = first / plan['files'][0]['path']
    inode = file.stat().st_ino
    from mycelium_a13_macos import slice_acquisition as slices
    def forbidden(*a, **k):
        pytest.fail('verified rollover slice must not fetch segments again')
    monkeypatch.setattr(slices, '_fetch_segment', forbidden)
    second = start_slice(app, agent, state, plan, run='second')
    assert (second / plan['files'][0]['path']).stat().st_ino == inode
    assert not first.exists()
    assert len(calls) == 2  # fresh signed descriptor per run, never replayed


@pytest.mark.parametrize('change', ['key', 'generation', 'incarnation', 'verification_key_digest', 'plan'])
def test_changed_binding_cannot_reuse_previous_slice(world, change):
    app, agent, gate, state, calls = world
    identity = setup_binding(app)
    plan = node_plan()
    first = start_slice(app, agent, state, plan)
    # A sentinel is not part of the plan: a fresh run must not inherit it.
    (first / 'sentinel').write_text('old')
    key = 'a' * 64
    if change == 'key': key = 'b' * 64
    elif change == 'plan': plan = node_plan(b'new bytes')
    elif change == 'generation': identity[change] += 1
    else: identity[change] += '-new'
    second = start_slice(app, agent, state, plan, key=key, run='second')
    assert not (second / 'sentinel').exists()
    assert not first.exists()


def test_withdrawn_consent_never_reuses_or_accepts_new_run(world):
    app, agent, gate, state, calls = world
    setup_binding(app)
    plan = node_plan()
    first = start_slice(app, agent, state, plan)
    gate.reason = 'standing_consent_withdrawn'
    state['a'] = announcement('collect', run_id='second')
    assert agent.tick()['state'] == 'waiting_for_consent'
    assert first.exists()
    assert len(calls) == 1
