# SPDX-License-Identifier: AGPL-3.0-only
"""Later-tick retry is offer-bound, typed and restricted to pre-launch staging."""
import copy
import json
from types import SimpleNamespace

import pytest

from mycelium_a13_macos.managed_control import ManagedControlError
from mycelium_a13_macos.standing_consent import (
    StandingAcceptor, bind_standing_consent, grant_standing_consent,
    withdraw_standing_consent,
)
from test_standing_consent import Actor, BINDING, GRANT, NOW, OFFER, POLICY, PREVIEW
from test_demo_target import world, announcement, offer

CODES = ['managed_control_remote_error', 'managed_control_unreachable',
         'managed_control_response_timeout', 'relay_backend_unavailable']
READ = '/managed-control/staging/read'
ACK = '/managed-control/staging/ack'


def setup_acceptor(root, *, where='preview', code=CODES[0], path=READ, persistent=False):
    grant_standing_consent(root, GRANT, policy=POLICY, now=NOW)
    bind_standing_consent(root, BINDING, now=NOW)
    calls = []
    clock = [NOW]
    def step(name, result):
        def run(*args, **kwargs):
            calls.append(name)
            if name == where and (persistent or calls.count(name) == 1):
                raise ManagedControlError(code, status=502, request_path=path)
            return copy.deepcopy(result)
        return run
    acceptor = StandingAcceptor(root, preview=step('preview', PREVIEW),
        accept=step('accept', {'state': 'running'}), clock=lambda: clock[0])
    actor = Actor(dict(OFFER))
    tick = lambda: acceptor.tick(actor, policy=POLICY, binding=BINDING, resource_check=lambda: None)
    return acceptor, actor, tick, calls, clock


@pytest.mark.parametrize('where', ['preview', 'accept'])
@pytest.mark.parametrize('path', [READ, ACK])
@pytest.mark.parametrize('code', CODES)
def test_transient_staging_error_accepts_on_later_tick(tmp_path, where, path, code):
    acceptor, actor, tick, calls, clock = setup_acceptor(tmp_path, where=where, code=code, path=path)
    original_offer = dict(actor.offer)
    assert tick()['state'] == 'failed'
    clock[0] += 2
    result = tick()
    assert result is not None and result['state'] == 'accepted'
    assert calls.count(where) == 2
    assert actor.offer == original_offer
    assert clock[0] < actor.offer['expires_at']
    assert tick() is None
    assert acceptor.status()['last'].get('error_http') is None


@pytest.mark.parametrize('path', [None, '/managed-control/poll', '/managed-control/token-progress',
                                  READ + '?token=PRIVATE', '/managed-control/staging/cleanup/ack'])
def test_unproven_substeps_remain_terminal(tmp_path, path):
    _, _, tick, calls, _ = setup_acceptor(tmp_path, where='accept', path=path)
    assert tick()['state'] == 'failed'
    assert tick() is None
    assert calls == ['preview', 'accept']


@pytest.mark.parametrize('code', ['offer_ack_conflict', 'offer_changed', 'managed_control_signature_invalid',
                                  'managed_control_command_interrupted', 'stage_runtime_active'])
def test_nontransient_codes_remain_terminal(tmp_path, code):
    _, _, tick, calls, _ = setup_acceptor(tmp_path, code=code)
    assert tick()['reason'] == code
    assert tick() is None
    assert calls == ['preview']


@pytest.mark.parametrize('expires', ['missing', None, float('inf'), float('nan'), '9e9', True])
def test_offer_without_a_known_expiry_is_never_retried(tmp_path, expires):
    """Retry is bounded only by the offer's own expiry: no finite expiry, no retry."""
    _, actor, tick, calls, _ = setup_acceptor(tmp_path)
    if expires == 'missing':
        del actor.offer['expires_at']
    else:
        actor.offer['expires_at'] = expires
    result = tick()
    assert result['state'] == 'failed' and result['reason'] == CODES[0]
    assert tick() is None
    assert calls == ['preview']


def test_expiry_bounds_repeated_failures_without_renewing_offer(tmp_path):
    _, actor, tick, calls, clock = setup_acceptor(tmp_path, persistent=True)
    assert tick()['state'] == 'failed'
    clock[0] = OFFER['expires_at'] - 1
    assert tick()['state'] == 'failed'
    clock[0] = OFFER['expires_at']
    assert tick()['reason'] == 'offer_expired'
    assert tick() is None
    assert calls == ['preview', 'preview']
    assert actor.offer['expires_at'] == OFFER['expires_at']


def test_expiry_during_preview_never_starts_accept(tmp_path):
    acceptor, _, tick, calls, clock = setup_acceptor(tmp_path)
    def preview(*args, **kwargs):
        clock[0] = OFFER['expires_at']
        return copy.deepcopy(PREVIEW)
    acceptor._preview = preview
    assert tick()['reason'] == 'offer_expired'
    assert calls == []


def test_retry_rechecks_withdrawal(tmp_path):
    _, _, tick, calls, _ = setup_acceptor(tmp_path)
    assert tick()['state'] == 'failed'
    withdraw_standing_consent(tmp_path, now=NOW + 1)
    assert tick()['reason'] == 'standing_consent_withdrawn'
    assert calls == ['preview']


def test_attached_runtime_is_never_retried_or_stopped(tmp_path):
    acceptor, actor, tick, calls, _ = setup_acceptor(tmp_path, where='accept')
    runtime = object()
    def accept(*args, **kwargs):
        calls.append('accept')
        actor._runtime = runtime
        raise ManagedControlError(CODES[0], status=502, request_path=READ)
    acceptor._accept = accept
    assert tick()['state'] == 'failed'
    assert tick() is None
    assert actor._runtime is runtime
    assert calls == ['preview', 'accept']


def test_untyped_transport_code_is_not_retry_permission(tmp_path):
    acceptor, _, tick, calls, _ = setup_acceptor(tmp_path)
    def fail(*args, **kwargs):
        calls.append('preview')
        raise ValueError(CODES[0])
    acceptor._preview = fail
    assert tick()['reason'] == CODES[0]
    assert tick() is None
    assert calls == ['preview']


@pytest.mark.parametrize('path,expected', [(READ, READ), (READ+'?token=PRIVATE', None),
    ('https://PRIVATE/managed-control/staging/read', None), ('/PRIVATE', None)])
def test_standing_http_diagnostics_are_bounded_detached_and_cleared(tmp_path, path, expected):
    acceptor, _, tick, _, _ = setup_acceptor(tmp_path, path=path)
    result = tick()
    wanted = {'status': 502, 'path': expected, 'code': CODES[0]}
    assert result['error_http'] == wanted
    result['error_http']['status'] = 599
    assert acceptor.status()['last']['error_http'] == wanted
    status = acceptor.status()
    status['last']['error_http']['status'] = 598
    assert acceptor.status()['last']['error_http'] == wanted
    assert 'PRIVATE' not in json.dumps(acceptor.status())


def test_joined_demo_projects_only_matching_standing_failure(world):
    app, agent, _, state, _ = world
    state['a'] = announcement('work')
    app.authority.offer = offer('work-1')
    last = {'offer_id': 'work-1', 'state': 'failed', 'reason': CODES[0],
            'error_http': {'status': 502, 'path': READ, 'code': CODES[0]}}
    app.standing = SimpleNamespace(status=lambda: {'last': copy.deepcopy(last)})
    result = agent.tick()
    assert result['detail'] == CODES[0]
    assert result['error_http'] == last['error_http']
    from mycelium_a13_macos.app import _demo_status
    app.installation_role = 'target'
    app.demo_target = agent
    assert _demo_status(app, 'target')['error_http'] == last['error_http']
    last['offer_id'] = 'old-offer'
    assert agent.tick()['error_http'] is None
    last['offer_id'], last['state'] = 'work-1', 'accepted'
    assert agent.tick()['error_http'] is None


def test_outer_standing_loop_exposes_and_clears_http_metadata(tmp_path):
    import threading
    from mycelium_a13_macos.app import DemoApp
    class Once:
        def __init__(self): self.calls = 0
        def wait(self, seconds):
            self.calls += 1
            return self.calls > 1
        def is_set(self): return False
    def fail():
        raise ManagedControlError(CODES[0], status=502, request_path=READ)
    app = SimpleNamespace(root=tmp_path, installation_role='target', running=True,
        closed=Once(), guard=threading.RLock(), _standing_tick=fail, standing=None,
        standing_error=None, last_report=None, _load_join_state=lambda: None)
    DemoApp._standing_loop(app)
    result = DemoApp._join_flow_status(app)
    assert result['standing_error'] == CODES[0]
    assert result['standing_error_http'] == {'status': 502, 'path': READ, 'code': CODES[0]}
    result['standing_error_http']['status'] = 599
    assert DemoApp._join_flow_status(app)['standing_error_http']['status'] == 502
    app.closed, app._standing_tick = Once(), lambda: None
    DemoApp._standing_loop(app)
    result = DemoApp._join_flow_status(app)
    assert result['standing_error'] is None and result['standing_error_http'] is None


def test_expiry_during_pre_run_report_prevents_preview(tmp_path):
    acceptor, _, tick, calls, clock = setup_acceptor(tmp_path)
    def report(*args): clock[0] = OFFER['expires_at']
    acceptor._report = report
    assert tick()['reason'] == 'offer_expired'
    assert calls == []


def test_transient_vocabulary_and_download_policy_are_explicit():
    from mycelium_a13_macos.control_failures import TRANSIENT_CONTROL_CODES
    from mycelium_a13_macos.demo_target import _DOWNLOAD_RETRYABLE
    assert TRANSIENT_CONTROL_CODES == frozenset(CODES)
    assert _DOWNLOAD_RETRYABLE == frozenset({
        'network_error', 'source_disconnected', 'source_unavailable', 'source_incomplete',
        'managed_control_unreachable', 'managed_control_remote_error', 'managed_control_response_timeout',
        'model_source_busy', 'acquisition_failed'})
