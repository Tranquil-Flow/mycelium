# SPDX-License-Identifier: AGPL-3.0-only
"""The activation SEQUENCING defect: a control call issued before the app reports readiness.

Measured, 2026-09-20, four rounds died at `STEP_FAILED start_and_activate` (p82r1, p82r2, p81r2, p82r3).
p82r1 and p82r3 died inside the FIRST `/api/serving/status` read of the step
(`round14_start_and_activate.py:12`) as `handshake_timeout:...:attempts=16:TimeoutError('timed out')` --
while the app was ALIVE: its surface stayed present and it answered `/api/status` and `/api/quit` minutes
later. The app does not answer inside `app_rpc.py`'s FIXED 10 s read deadline while it compiles and then
LOADS a 14 GB model, and `_client`'s busy retry is itself bounded, so whether a round survived was decided
by which budget expired first -- a property of the clock, not of the app's state.

WHAT THIS FILE PINS. The round must WAIT ON THE APP'S OWN READINESS REPORT before it issues a control
call, and must never issue one whose own precondition the app does not satisfy:

  * `/api/serving/start`    is accepted only while `compilation.state == 'compiled'`   (chain_compile.py:83)
  * `/api/serving/activate` is accepted only while `serving.state == 'awaiting_activation'`
                                                                                      (chain_compile.py:116)

`route_ready` and `serving_role` are NOT signals and are rejected in the fix's own comment: `route_ready`
is hardcoded False on every `/api/serving/*` return (`mycelium_a13_macos/app.py:445`) and `serving_role`
is not in the status payload at all -- it exists only in the launcher's startup event
(`mycelium_a13_macos/__main__.py:59`).

Nothing here tests a widened timeout, because nothing was widened: the last two tests pin that the
transport's own budgets and the pinned step are untouched.
"""
import importlib.util
import os
import pathlib
import re

import pytest

OPERATOR = pathlib.Path(__file__).resolve().parents[2] / 'scripts/a13_operator'
for key, value in (('A13_OWNER_ROOT', '/tmp/a13-readiness-owner'),
                   ('A13_REMOTE_ROOT', '/tmp/a13-readiness-remote'),
                   ('A13_TARGET_PREVIEW', 'preview82'),
                   ('A13_RPC_ROUND', 'rpc-readiness')):
    os.environ.setdefault(key, value)
pathlib.Path('/tmp/a13-readiness-owner').mkdir(parents=True, exist_ok=True)

_spec = importlib.util.spec_from_file_location('round14_rpc_readiness', OPERATOR / 'round14_rpc.py')
rpc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rpc)

STATUS = '/api/serving/status'
START = '/api/serving/start'
ACTIVATE = '/api/serving/activate'
START_BODY = {'execution_proposal': {'max_wall_seconds': 1800}, 'model_execution_acknowledged': True}
ACTIVATE_BODY = {'execution_proposal': {'max_wall_seconds': 1800}, 'activation_acknowledged': True}


def compiling():
    return {'compilation': {'state': 'compiling'}}


def compiled():
    return {'compilation': {'state': 'compiled', 'execution_proposal': {'max_wall_seconds': 1800}}}


def serving(state, compilation='compiled'):
    return {'compilation': {'state': compilation}, 'serving': {'state': state}}


class FakeApp:
    """The app's own control surface, scripted: what it reports, and when it cannot answer at all.

    `reports`            consumed by each `/api/serving/status` read; the LAST entry repeats.
    `handshakes`         exceptions raised by each `_client(root)` -- the p82r1/p82r3 shape, where the
                         app's surface exists but the native-session handshake (app_rpc.py:15's FIXED
                         10 s) expires. When the list is empty the surface answers.
    `control_answer`     what a control call returns.

    Every successful call is recorded TOGETHER WITH the app's own last report at that moment, so a test can
    ask the only question that matters: was the app reporting readiness when that control call was issued?
    """

    def __init__(self, *, reports=(), handshakes=(), control_answer=lambda path: {'ok': path}):
        self.reports = list(reports)
        self.handshakes = list(handshakes)
        self.control_answer = control_answer
        self.calls = []
        self.handshake_attempts = 0
        self.last_report = None

    def client(self, root):
        self.handshake_attempts += 1
        if self.handshakes:
            raise self.handshakes.pop(0)
        return _Client(self)

    def answer(self, path):
        self.calls.append({'path': path,
                           'reported_compilation': _state(self.last_report, 'compilation'),
                           'reported_serving': _state(self.last_report, 'serving')})
        if path == STATUS:
            self.last_report = self._next_report()
            return self.last_report
        return self.control_answer(path)

    def _next_report(self):
        if len(self.reports) > 1:
            return self.reports.pop(0)
        return self.reports[0] if self.reports else {}


class _Client:
    def __init__(self, app):
        self.app = app

    def call(self, path, body=None):
        return self.app.answer(path)


def _state(report, section):
    value = (report or {}).get(section)
    return value.get('state') if isinstance(value, dict) else None


def _wire(monkeypatch, app, *, clock=None, budget=None):
    """Wire a scripted app into the transport; no sockets, no sleeps, no clock drift."""
    monkeypatch.setattr(rpc, 'require_local', lambda root: None)
    monkeypatch.setattr(rpc, '_client', app.client)
    monkeypatch.setattr(rpc.time, 'sleep', lambda s: clock.__setitem__('t', clock['t'] + s) if clock else None)
    if clock is not None:
        monkeypatch.setattr(rpc.time, 'monotonic', lambda: clock['t'])
    if budget is not None:
        monkeypatch.setattr(rpc, '_READINESS_BUDGET_S', budget, raising=False)
    rpc._OWNER_ANSWERED.clear()


def test_a_start_call_is_not_issued_until_the_app_reports_compiled(monkeypatch):
    """The pinned step issues `/api/serving/start` the instant its compile loop breaks. It must not."""
    app = FakeApp(reports=[compiling(), compiling(), compiled()])
    _wire(monkeypatch, app)

    rpc.owner(START, START_BODY)

    paths = [entry['path'] for entry in app.calls]
    assert paths == [STATUS, STATUS, STATUS, START], (
        'the control call must follow the app\'s own readiness reports, never precede them')
    issued = app.calls[-1]
    assert issued['reported_compilation'] == 'compiled', (
        'the app must have reported the state its own handler requires (chain_compile.py:83)')


def test_an_activate_call_is_not_issued_until_the_app_reports_awaiting_activation(monkeypatch):
    """`activate_serving` refuses anything but `awaiting_activation` (chain_compile.py:116)."""
    app = FakeApp(reports=[serving('staging'), serving('awaiting_activation')])
    _wire(monkeypatch, app)

    rpc.owner(ACTIVATE, ACTIVATE_BODY)

    paths = [entry['path'] for entry in app.calls]
    assert paths == [STATUS, STATUS, ACTIVATE]
    assert app.calls[-1]['reported_serving'] == 'awaiting_activation'


def test_a_busy_app_does_not_end_the_round_before_it_reports_readiness(monkeypatch):
    """p82r1/p82r3's shape: the surface exists but the handshake expires while the app is busy.

    Today `_client`'s bounded budget ends the round there (`HandshakeTimeout`, attempts=16). The app is
    ALIVE -- the round must keep waiting on its own readiness report instead.
    """
    busy = rpc.HandshakeTimeout('handshake_timeout:%s:attempts=16:TimeoutError(\'timed out\')' % rpc.OWNER)
    app = FakeApp(reports=[compiled()], handshakes=[busy, busy])
    _wire(monkeypatch, app)

    rpc.owner(START, START_BODY)

    assert [entry['path'] for entry in app.calls] == [STATUS, START]
    assert app.calls[-1]['reported_compilation'] == 'compiled'


def test_a_waiting_round_says_so_in_its_own_log(monkeypatch, capsys):
    """The wait must be visible in the round log; a silent wait is unfalsifiable."""
    app = FakeApp(reports=[compiling(), compiled()])
    _wire(monkeypatch, app)

    rpc.owner(START, START_BODY)

    printed = capsys.readouterr().out
    assert 'readiness_wait' in printed
    assert 'path=%s' % START in printed


def test_a_ready_app_costs_exactly_one_extra_read(monkeypatch):
    """The healthy path must stay cheap: one cheap read, then the call."""
    app = FakeApp(reports=[compiled()])
    _wire(monkeypatch, app)

    rpc.owner(START, START_BODY)

    assert [entry['path'] for entry in app.calls] == [STATUS, START]


def test_an_app_that_reports_failure_refuses_the_call_by_name(monkeypatch):
    """A terminal report is the app's own verdict: refuse at once, and issue NOTHING."""
    app = FakeApp(reports=[{'compilation': {'state': 'failed', 'error_code': 'chain_compile_failed'}}])
    _wire(monkeypatch, app)

    with pytest.raises(RuntimeError) as excinfo:
        rpc.owner(START, START_BODY)

    assert 'readiness_refused' in str(excinfo.value)
    assert START in str(excinfo.value)
    assert [entry['path'] for entry in app.calls] == [STATUS], 'no control call may be issued'


def test_the_readiness_wait_is_bounded_and_ends(monkeypatch):
    """Bounded, not infinite: an app that never reports readiness must end the round, not hang it."""
    app = FakeApp(reports=[compiling()])
    clock = {'t': 0.0}
    _wire(monkeypatch, app, clock=clock, budget=30.0)

    with pytest.raises(RuntimeError) as excinfo:
        rpc.owner(START, START_BODY)

    assert 'readiness_timeout' in str(excinfo.value)
    assert [entry['path'] for entry in app.calls] == [STATUS] * len(app.calls)
    assert len(app.calls) <= 8, 'the wait ended rather than polling forever'


def test_a_non_busy_failure_is_still_raised_at_once(monkeypatch):
    """A bad credential, a dead socket or a refusal mid-run must NOT be waited on. Unchanged."""
    calls = {'n': 0}

    class BadCredential:
        def call(self, path, body=None):
            calls['n'] += 1
            raise PermissionError('bad credential')

    monkeypatch.setattr(rpc, 'require_local', lambda root: None)
    monkeypatch.setattr(rpc, '_client', lambda root: BadCredential())
    monkeypatch.setattr(rpc.time, 'sleep', lambda s: None)
    rpc._OWNER_ANSWERED.clear()

    with pytest.raises(PermissionError):
        rpc.owner(START, START_BODY)
    assert calls['n'] == 1


def test_the_gate_does_not_gate_the_read_it_uses(monkeypatch):
    """No recursion: the readiness read is the probe, so it cannot itself be gated."""
    assert STATUS not in rpc._READINESS
    assert set(rpc._READINESS) == {START, ACTIVATE}


def test_the_work_consent_calls_keep_the_pinned_sequencing(monkeypatch):
    """Deliberately NOT gated: the pinned step already waits on the app's own offer report for these.

    `round14_start_and_activate.py:26-28` polls `/api/staging/check` until the app itself publishes the
    offer, and `/api/work/preview` refuses with `work_offer_missing` without one (app.py:553-554). A
    second gate here would add target RPCs (wp8-03 item 2) for no new information.
    """
    assert '/api/work/preview' not in rpc._READINESS
    assert '/api/work/accept' not in rpc._READINESS
    assert '/api/staging/check' not in rpc._READINESS


def test_the_two_declared_bounds_the_readiness_budget_is_derived_from_still_agree():
    """The budget is DERIVED from the app's own declaration, not picked (the house rule).

    1. the app's own wall bound for this run: `RESOURCE_PROPOSAL.max_wall_seconds` (serving.py:91);
    2. the pinned step's own declared patience for this very wait: `deadline=time.monotonic()+1800`
       (round14_start_and_activate.py:10).
    """
    serving_source = (pathlib.Path(__file__).resolve().parents[2]
                      / 'mycelium_a13_macos/serving.py').read_text()
    app_max_wall = int(re.search(r'max_wall_seconds=([\d_]+)', serving_source).group(1).replace('_', ''))
    step = (OPERATOR / 'round14_start_and_activate.py').read_text()
    step_deadline = int(re.search(r'deadline=time\.monotonic\(\)\+(\d+)', step).group(1))
    assert app_max_wall == step_deadline == int(rpc._READINESS_BUDGET_S), (
        'the readiness budget must stay the wall bound the app itself declares')


def test_no_existing_budget_window_or_guard_was_changed(monkeypatch):
    """The laws: the transport's own budgets and the pinned client are untouched by this change."""
    assert rpc._OWNER_CALL_BUDGET_S == 120.0
    assert rpc._OWNER_STARTUP_GRACE_S == 20.0
    app_rpc = (OPERATOR / 'app_rpc.py').read_text()
    assert app_rpc.count('timeout=10') == 2, 'app_rpc.py\'s fixed 10 s read deadline is untouched'
    assert 'timeout=300' in app_rpc
    step = (OPERATOR / 'round14_start_and_activate.py').read_text()
    assert "owner('/api/serving/status',{})" in step, 'the pinned step still polls the app itself'
    assert 'readiness' not in step, 'the fix must not live in the pinned step'


def test_the_read_path_itself_keeps_its_existing_failure_semantics(monkeypatch):
    """The readiness READ is not made patient here: a permanently busy app still fails, bounded, by name.

    Making that read outlive `_client`'s own budget would change a guard, not a sequence -- and
    `tests/a13_macos/test_operator_handshake_retry.py` pins today's semantics. It is recorded as an
    escalation instead (see the commit message), so this test pins that it was NOT taken silently.
    """
    import json

    surface = rpc.OWNER / 'native-control.json'
    surface.write_text(json.dumps({'port': 50000, 'credential': 'x'}))
    surface.chmod(0o600)
    monkeypatch.setenv('A13_HANDSHAKE_DEADLINE_S', '1')
    monkeypatch.setenv('A13_HANDSHAKE_RETRY_SLEEP_S', '0.01')
    seen = {'n': 0}

    class AlwaysBusy:
        def __init__(self, root):
            seen['n'] += 1
            raise TimeoutError('timed out')

    monkeypatch.setattr(rpc, 'AppClient', AlwaysBusy)
    monkeypatch.setattr(rpc, 'require_local', lambda root: None)
    rpc._OWNER_ANSWERED.clear()

    with pytest.raises(rpc.HandshakeTimeout) as excinfo:
        rpc.owner(STATUS, {})

    assert 'handshake_timeout' in str(excinfo.value), 'the busy read still fails by its own name'
    assert seen['n'] > 1, 'the read path keeps its own bounded retry, unchanged'
    assert seen['n'] < 200, 'and it stays a deadline'
