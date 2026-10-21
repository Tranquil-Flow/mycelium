# SPDX-License-Identifier: AGPL-3.0-only
"""The boot-id collection must be RETRIED inside one bounded budget, and still fail inert.

WHY THIS FILE EXISTS
    Round p80r1 (2026-09-20) refused its only route with node_hello_identity_invalid /
    kind='boot_id_mismatch' at physical_inference_qualification.py:1720 -- the plan's bound value
    (boot-615e5402...) came from the round's OWN live hello at 11:44:14 while the node's live value at
    11:56:46 was boot-424573b4..., i.e. the node's identity moved AFTER the round observed it. The
    app-side refresh that exists to prevent exactly this never fired: its collection fired ONCE, early,
    and raised ManagedObservationsError('managed_control_response_timeout') because the node was not
    yet answering on this run (log line: "boot_id_observation_unavailable
    ManagedObservationsError('managed_control_response_timeout')"). It has never once succeeded in any
    round of this program (scripts/a13_operator/bootid_guard.py: the success line has never been seen).

    The producer's refusal is NOT a verdict about this run: it says the control channel was not up yet.
    So the collection is retried inside ONE bounded budget -- never by widening a per-command timeout --
    and the classes stay exactly as they are: ManagedObservationsError remains the only expected refusal
    (the inert default), and a programming error must still be loud and is never retried.

    Only refusals the clock can fix are retried. A refusal that is a verdict about THIS caller's own
    inputs -- a malformed or stale run id, a mismatched factory, no nodes -- is answered the same way
    forever, so it reaches the inert default at once: measured, tests/a13_macos/test_serving_workflow.py
    drives the REAL collector, is refused with managed_observation_run_id_invalid, and spent minutes in
    this loop before that distinction was made.

    These tests drive the real prepare_serving_workflow with the collector replaced at the module
    boundary (the injection point managed_observations.py itself blesses) and the bounded wait replaced
    by a deterministic clock, so the retry schedule is pinned without ever sleeping.
"""
import json

import pytest
from test_assignment_planning import enrolled, seed  # noqa: F401 -- `seed` is the fixture `enrolled` needs
from test_workflow_observations_shipping import _fake_document, _prepared_plan
from mycelium_a13_macos.managed_observations import ManagedObservationsError
from mycelium_a13_macos.workflow import owner_channel, prepare_serving_workflow
from mycelium_a13_macos import workflow as workflow_module


def _bounded_wait(monkeypatch):
    """Deterministic clock + recording no-op wait; returns the shared state.

    The module attributes are replaced with raising=False so the same tests show a real behavioural
    failure (one attempt, no retry) rather than an AttributeError while the seam is missing.
    """
    state = {'now': 0.0, 'sleeps': []}
    monkeypatch.setattr(workflow_module, '_boot_id_observation_clock', lambda: state['now'],
                        raising=False)

    def wait(seconds):
        state['sleeps'].append(seconds)
        state['now'] += seconds

    monkeypatch.setattr(workflow_module, '_boot_id_observation_wait', wait, raising=False)
    return state


def _budget():
    return workflow_module._BOOT_ID_OBSERVATION_BUDGET_SECONDS


def _prepare(authority, prepared, plan, roots):
    channel = owner_channel(authority)
    try:
        return channel, prepare_serving_workflow(authority=authority, channel=channel, prepared=prepared,
                                                operator_plan=plan, target_source_roots=roots)
    except Exception:
        channel.close()
        raise


def test_the_collection_is_retried_inside_its_budget_and_then_binds_the_live_hello(tmp_path, enrolled,
                                                                                  monkeypatch, capsys):
    """A channel that answers on the third attempt must still bind its LIVE boot ids.

    RED while the collection is single-shot: the first ManagedObservationsError ends it, the peers keep
    the values the plan document carried, and no success line is printed -- the p80r1 shape.
    """
    authority, nodes, prepared, plan, roots, document = _prepared_plan(tmp_path, enrolled)
    peer_nodes = [peer['node_id'] for peer in document['controller']['peers']]
    run_plan = document['controller']['run_plan']
    attempts = []

    def flaky_collect(factory, *, run_id, deployment_id, node_ids):
        attempts.append(list(node_ids))
        if len(attempts) < 3:
            raise ManagedObservationsError('managed_control_response_timeout')
        return _fake_document(run_id, deployment_id, node_ids)

    monkeypatch.setattr('mycelium_a13_macos.managed_observations.collect_managed_hello_observations',
                        flaky_collect)
    state = _bounded_wait(monkeypatch)
    channel, workflow = _prepare(authority, prepared, plan, roots)
    try:
        assert len(attempts) == 3, (
            'the collection was not retried inside its bounded budget: %d attempt(s) for a channel that '
            'answered on the third -- the p80r1 single-shot shape' % (len(attempts),))
        assert all(ids == peer_nodes for ids in attempts)
        assert len(state['sleeps']) == 2, 'one wait between retries: %r' % (state['sleeps'],)
        assert all(0.0 < delay <= workflow_module._BOOT_ID_OBSERVATION_MAX_RETRY_SECONDS
                   for delay in state['sleeps']), 'unbounded wait: %r' % (state['sleeps'],)
        assert sum(state['sleeps']) <= _budget(), 'the retries overran the budget: %r' % (state['sleeps'],)
        assert state['now'] < _budget(), 'the retry budget must not be consumed before it is needed'
        bound = json.loads(workflow.launch.operator_plan.read_text())
        expected = {item['node_id']: item['boot_id'] for item in
                    _fake_document(run_plan['run_id'], run_plan['deployment_id'], peer_nodes)['observations']}
        actual = {peer['node_id']: peer.get('boot_id') for peer in bound['controller']['peers']}
        assert actual == expected, 'the retried observation did not reach the bound plan: %r' % (actual,)
        out = capsys.readouterr().out
        assert 'boot_id_observations_collected nodes=%d' % len(peer_nodes) in out, out
        assert 'boot_id_observation_unavailable' not in out, out
        # The refresh is reported from the caller and NEVER stored in the plan: the controller field set
        # is closed, and an extra field refuses the whole document (plan_unknown_field).
        assert 'boot_id_bindings' not in bound['controller']
        from mycelium_physical_runner.config import load_operator_plan
        load_operator_plan(workflow.launch.operator_plan)
    finally:
        channel.close()


def test_an_exhausted_budget_still_fails_inert_and_never_as_a_defect(tmp_path, enrolled, monkeypatch,
                                                                    capsys):
    """The budget is bounded, and exhausting it is the documented inert default -- not a defect.

    Pins both halves of the requirement: the collection IS retried (more than one attempt, waits between
    them, all inside the budget and never widening a per-command timeout), and when the budget is spent
    the round still proceeds byte-for-byte as before, with the producer's own refusal reported.
    """
    authority, nodes, prepared, plan, roots, document = _prepared_plan(tmp_path, enrolled)
    before = {peer['node_id']: peer.get('boot_id') for peer in document['controller']['peers']}
    attempts = []

    def silent_collect(factory, *, run_id, deployment_id, node_ids):
        attempts.append(list(node_ids))
        raise ManagedObservationsError('managed_control_response_timeout')

    monkeypatch.setattr('mycelium_a13_macos.managed_observations.collect_managed_hello_observations',
                        silent_collect)
    state = _bounded_wait(monkeypatch)
    channel, workflow = _prepare(authority, prepared, plan, roots)
    try:
        assert len(attempts) >= 2, (
            'the collection was attempted %d time(s): an exhausted budget must show retries, not a '
            'single early attempt (p80r1)' % (len(attempts),))
        assert len(state['sleeps']) == len(attempts) - 1, (state['sleeps'], attempts)
        assert sum(state['sleeps']) <= _budget(), 'the retries overran the budget: %r' % (state['sleeps'],)
        for delay in state['sleeps']:
            assert 0.0 < delay <= workflow_module._BOOT_ID_OBSERVATION_MAX_RETRY_SECONDS, state['sleeps']
        bound = json.loads(workflow.launch.operator_plan.read_text())
        after = {peer['node_id']: peer.get('boot_id') for peer in bound['controller']['peers']}
        assert after == before, 'an unavailable observation changed the bound peers: %r' % (after,)
        out = capsys.readouterr().out
        assert ("boot_id_observation_unavailable "
                "ManagedObservationsError('managed_control_response_timeout')") in out, out
        assert 'UnboundLocalError' not in out and 'NameError' not in out, (
            'a refusal must never be reported in the defect vocabulary: %r' % (out,))
    finally:
        channel.close()


def test_a_verdict_about_our_own_inputs_is_inert_at_once_and_spends_no_budget(tmp_path, enrolled,
                                                                              monkeypatch, capsys):
    """A refusal the clock cannot fix is still inert -- but it must not cost the budget.

    Measured in the pinned environment: tests/a13_macos/test_serving_workflow.py drives the REAL collector
    and is refused with managed_observation_run_id_invalid. That run id is the same forever, so retrying
    cannot change the answer; waiting out the budget on it turned a 2.7 s file into minutes.
    """
    authority, nodes, prepared, plan, roots, document = _prepared_plan(tmp_path, enrolled)
    before = {peer['node_id']: peer.get('boot_id') for peer in document['controller']['peers']}
    attempts = []

    def refused_collect(factory, *, run_id, deployment_id, node_ids):
        attempts.append(list(node_ids))
        raise ManagedObservationsError('managed_observation_run_id_invalid')

    monkeypatch.setattr('mycelium_a13_macos.managed_observations.collect_managed_hello_observations',
                        refused_collect)
    state = _bounded_wait(monkeypatch)
    channel, workflow = _prepare(authority, prepared, plan, roots)
    try:
        assert len(attempts) == 1, 'a verdict about our own inputs was retried: %r' % (attempts,)
        assert state['sleeps'] == [], 'a verdict about our own inputs waited: %r' % (state['sleeps'],)
        bound = json.loads(workflow.launch.operator_plan.read_text())
        after = {peer['node_id']: peer.get('boot_id') for peer in bound['controller']['peers']}
        assert after == before, 'a refused observation changed the bound peers: %r' % (after,)
        out = capsys.readouterr().out
        assert ("boot_id_observation_unavailable "
                "ManagedObservationsError('managed_observation_run_id_invalid')") in out, out
        assert 'UnboundLocalError' not in out and 'NameError' not in out, out
    finally:
        channel.close()


def test_a_programming_error_is_never_retried_and_is_never_reported_as_unavailable(tmp_path, enrolled,
                                                                                  monkeypatch, capsys):
    """A defect must stay loud: exactly one attempt, no wait, no inert refusal line.

    Measured 2026-09-19 (soak04): a function-local import made every call raise UnboundLocalError, and a
    bare `except Exception` reported it as boot_id_observation_unavailable for an entire session.
    """
    authority, nodes, prepared, plan, roots, document = _prepared_plan(tmp_path, enrolled)
    attempts = []

    def broken_collect(factory, *, run_id, deployment_id, node_ids):
        attempts.append(list(node_ids))
        raise UnboundLocalError("cannot access local variable 'collect_managed_hello_observations'"
                                " where it is not associated with a value")

    monkeypatch.setattr('mycelium_a13_macos.managed_observations.collect_managed_hello_observations',
                        broken_collect)
    state = _bounded_wait(monkeypatch)
    channel = owner_channel(authority)
    try:
        with pytest.raises(UnboundLocalError):
            prepare_serving_workflow(authority=authority, channel=channel, prepared=prepared,
                                     operator_plan=plan, target_source_roots=roots)
        assert len(attempts) == 1, 'a programming error must not be retried: %r' % (attempts,)
        assert state['sleeps'] == [], 'a programming error must not be waited on: %r' % (state['sleeps'],)
        out = capsys.readouterr().out
        assert 'boot_id_observation_unavailable' not in out, (
            'a defect was reported as the documented inert default: %r' % (out,))
    finally:
        channel.close()


def test_a_channel_that_is_already_up_is_collected_once_without_waiting(tmp_path, enrolled, monkeypatch,
                                                                       capsys):
    """The retry adds no latency to the path that already works: one attempt, no wait at all."""
    authority, nodes, prepared, plan, roots, document = _prepared_plan(tmp_path, enrolled)
    peer_nodes = [peer['node_id'] for peer in document['controller']['peers']]
    attempts = []

    def available_collect(factory, *, run_id, deployment_id, node_ids):
        attempts.append(list(node_ids))
        return _fake_document(run_id, deployment_id, node_ids)

    monkeypatch.setattr('mycelium_a13_macos.managed_observations.collect_managed_hello_observations',
                        available_collect)
    state = _bounded_wait(monkeypatch)
    channel, workflow = _prepare(authority, prepared, plan, roots)
    try:
        assert len(attempts) == 1, 'an available channel was polled %d times' % (len(attempts),)
        assert state['sleeps'] == [], 'an available channel was waited on: %r' % (state['sleeps'],)
        out = capsys.readouterr().out
        assert 'boot_id_observations_collected nodes=%d' % len(peer_nodes) in out, out
    finally:
        channel.close()


def test_a_duration_verdict_is_retried_because_a_new_attempt_gets_a_new_window(tmp_path, enrolled,
                                                                             monkeypatch, capsys):
    """`captured_stale` is NOT a verdict about our inputs: it is a verdict about how long THIS attempt took.

    Measured in the producer (mycelium_a13_macos/managed_observations.py): entry reads `now`
    (:176), exit reads `captured` (:262), and `_fresh_enough(now, captured)` (:85-87) admits only
    `-_MAX_SKEW_SECONDS <= now - captured <= _MAX_STALE_SECONDS`. Because `captured` is read LATER from the
    same clock, `now - captured <= 0` always, so the only failing branch is `captured - now >
    _MAX_SKEW_SECONDS` -- 60.0 seconds (:41). It therefore means "this attempt's collection took longer
    than 60 s", usually because a member's hello ran to its own 30 s budget while the node was still coming
    up: exactly the p80r1 shape. A retry re-reads `now` at its own entry, so the window is new each time and
    waiting CAN change the answer -- unlike run-id, deployment, factory and clock verdicts, which are pure
    functions of the caller's inputs.

    RED while `captured_stale` is in _BOOT_ID_OBSERVATION_TERMINAL_CODES: one attempt, no wait, and the
    binding stays inert even though a warm second attempt was available.
    """
    authority, nodes, prepared, plan, roots, document = _prepared_plan(tmp_path, enrolled)
    peer_nodes = [peer['node_id'] for peer in document['controller']['peers']]
    attempts = []

    def slow_then_ready(factory, *, run_id, deployment_id, node_ids):
        attempts.append(list(node_ids))
        if len(attempts) < 3:
            raise ManagedObservationsError('managed_observation_captured_stale')
        return _fake_document(run_id, deployment_id, node_ids)

    monkeypatch.setattr('mycelium_a13_macos.managed_observations.collect_managed_hello_observations',
                        slow_then_ready)
    state = _bounded_wait(monkeypatch)
    channel, workflow = _prepare(authority, prepared, plan, roots)
    try:
        assert len(attempts) == 3, (
            'a duration verdict was treated as final after %d attempt(s): it is measured fresh on every '
            'attempt, so waiting can change it' % (len(attempts),))
        assert len(state['sleeps']) == 2, 'one wait between retries: %r' % (state['sleeps'],)
        assert 'managed_observation_captured_stale' not in workflow_module._BOOT_ID_OBSERVATION_TERMINAL_CODES
        out = capsys.readouterr().out
        assert 'boot_id_observations_collected nodes=%d' % len(peer_nodes) in out, out
    finally:
        channel.close()
