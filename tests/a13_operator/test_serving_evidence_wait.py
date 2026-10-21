# SPDX-License-Identifier: AGPL-3.0-only
"""The serving wait is OBSERVATIONAL too: the round reads the app's OWN on-disk evidence, not an HTTP poll.

THE WALL, ONE PHASE LATER. `p83r1` (2026-09-21) died at `round14_start_and_activate.py:12` inside its
FIRST status read while the app compiled and answered nothing
(`handshake_timeout:...:attempts=16:TimeoutError('timed out')`), and that is fixed on this branch by
waiting on the compile's own on-disk terminal marker (`scripts/a13_operator/round14_rpc.py`,
`await_compile_end`). The SAME pinned step then waits for `serving.state == 'api_running'` by polling
that same read in a loop (`round14_start_and_activate.py:42-46`) while the app qualifies -- opens the
route and loads a 14 GB model -- and the compile fix's own "what this does not prove" names this wait
as NOT fixed and NOT claimed: "that phase has no on-disk success marker either (only
`serve-error.private.json`, chain_compile.py:96)". One read issued in that window spends the whole
bounded handshake budget and ends the round (`_client` retries, then `HandshakeTimeout`), which is the
wall's shape: which budget expires first, not the app's own state, decides the round.

WHAT THE APP WRITES ON ITS WAY TO `api_running` (read off source; every claim a file:line):

  * `mycelium_a13_macos/workflow.py:233-234` calls `self.launch.run(...)`, i.e.
    `RegistryLaunch.run` (`mycelium_a13_macos/serving.py:211-258`), whose runner is
    `_native_registry_runner` (`serving.py:493-496`) = `mycelium_live.supervisor.run_registry_server`.
  * `run_registry_server` (`supervisor.py:2211-2455`) QUALIFIES every plan first -- the route open and
    the model load, `_qualified_runtime` (`:2273-2286`) -- and only THEN builds the registry:
    `LiveDeploymentRegistry(..., state_path=registry_state, ...)` (`:2287-2292`).
  * that constructor's LAST statement persists the registry to disk (`mycelium_live/registry.py:146-149`
    -> `_persist`, `:785-815`: atomic temp file + fsync + `os.replace`), and `state_path` is never None
    on this path: `serving.py:599` sets `registry_state = root / "registry-state.json"`, `:608` passes
    it, with `private_state_root=root/'registry'` from `workflow.py:362` where
    `root = authority.state_root/'serving-runs'/run_id` (`workflow.py:357`).
  * only AFTER that does the HTTP server exist (`create_server`, `supervisor.py:2390-2404`) and only
    then does `on_started` (`:2427-2428`) run the callback that sets `_state='api_running'`
    (`workflow.py:210-214`) -- in memory, never persisted.

So `{A13_OWNER_ROOT}/authority/serving-runs/{run_id}/registry/registry-state.json` is the durable form
of the verdict the operator waits for: its ABSENCE cannot coexist with an app that reports
`api_running`, and it is written AFTER the qualification and BEFORE the remaining steps to
`api_running`, which are composition and a socket bind (`build_registry_stack`, `supervisor.py:2374-2382`
-> `:469-497`; `create_server`, `:2390-2404` -> `:1578-1617`) -- no A13 code in the serve thread holds
`app.guard` anywhere in this phase (the only `with app.guard` outside `app.py` is
`chain_compile.py:54`, which is the COMPILE). Measured, not only inferred: a real round's mtime
timeline lists this file at the same second as the pinned step's own terminal write
(`runs/wp8-02-useful-two-mac/RESULT-4.md:125`, produced by `round14_start_and_activate.py:47`).

WHAT THESE TESTS PIN.

  * the round SURVIVES a qualification that outlasts the handshake budget, and it does so by issuing NO
    RPC at all while the app cannot answer (the `handshake_refusals == 0` assertions);
  * the pre-fix transport -- the same pinned step with the observational wait disabled -- still dies at
    `round14_start_and_activate.py:43` by `handshake_timeout`, kept as a witness;
  * the marker is the app's own write, derived from the round's own scope record, and it is not fired
    by the earlier artifacts of the same directory (`registry/` and `registry/product/` exist from the
    COMPILE: `serving.py:596-598`);
  * the wait engages ONLY for a caller that has itself asked the app to activate, so every other read
    path -- the pre-activation loops, `await_ready`'s own probe, `/api/quit` -- is byte-for-byte
    unchanged, and the two budgets the wait is bounded by are the app's own declarations;
  * what it watches is a *prepared* serving run (`serving.py:596-598`, created while the compile still
    holds), so a run with no such root has nothing to observe and the wait steps aside ('absent').

RUN RED (the pre-fix tree, this file added but `await_serving_end` absent):
    env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV /opt/homebrew/bin/python3 -B -m pytest -q \\
        -p no:cacheprovider tests/a13_operator/test_serving_evidence_wait.py
"""

import ast
import importlib
import json
import pathlib
import sys
import threading
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
OPERATOR = REPO / 'scripts/a13_operator'
_REAL_SLEEP = time.sleep            # captured before any patch, so the simulation's own clock is real

# The app's own identifiers, quoted from the code that creates them:
RUN_ID = 'a13-qwen-20260921T101530Z'          # round14_bootstrap_compile.py:242
PROPOSAL = {'max_wall_seconds': 1800, 'run_count': 1,
            'max_combined_rss_bytes': 16_955_405_108,
            'max_additional_disk_bytes': 24_854_140_006}
QUALIFY_SECONDS = 2.0                         # a compressed qualification (route open + model load)
HANDSHAKE_BUDGET_S = 0.5                      # a compressed `A13_HANDSHAKE_DEADLINE_S` (real default 180)
# The wall's defining inequality, kept in the simulation: the qualification OUTLASTS the handshake
# budget, so the round cannot survive by retrying -- it has to stop asking.
assert QUALIFY_SECONDS > HANDSHAKE_BUDGET_S
STATUS = '/api/serving/status'
START = '/api/serving/start'
ACTIVATE = '/api/serving/activate'


class QualifyingApp:
    """The app AFTER activation: its control surface answers nothing, its disk keeps speaking.

    Two halves, both taken from the app's own source rather than invented:

    * the transport half -- `app_client` stands in for `app_rpc.AppClient` ONLY: while the app
      qualifies it raises the shape the real rounds recorded, a bare `TimeoutError('timed out')` out
      of `app_rpc.py:15`'s fixed read deadline (the app is ALIVE; it simply cannot answer). Everything
      above it is the real transport (`_client`'s bounded retry, `_busy_reason`, `HandshakeTimeout`).
    * the disk half -- the simulated app writes what the real app writes: the compile's marker
      (`workflow.py:357-360`), the `registry/` + `registry/product/` directories that already exist
      from the COMPILE (`serving.py:596-598`), and, when the qualification ends, the registry's own
      persisted state (`mycelium_live/registry.py:149`) BEFORE it starts answering again.
    """

    def __init__(self, owner_root, round_dir, *, qualify_seconds=QUALIFY_SECONDS):
        self.owner = pathlib.Path(owner_root)
        self.round_dir = pathlib.Path(round_dir)
        self.run_root = self.owner / 'chain-runs' / RUN_ID
        self.compile_marker = self.owner / 'authority' / 'serving-runs' / RUN_ID / 'operator-plan.json'
        self.serving_root = self.owner / 'authority' / 'serving-runs' / RUN_ID / 'registry'
        self.serving_marker = self.serving_root / 'registry-state.json'
        self.serving_error = self.run_root / 'serve-error.private.json'
        self.qualify_seconds = qualify_seconds
        self.answering = True
        self.qualifying = False
        self.serving = None
        self.staging_reads = 0
        self.activations = 0
        self.events = []
        self.calls = []
        self.handshake_attempts = 0      # every `app_rpc.AppClient(root)` construction
        self.handshake_refusals = 0      # ... and those made while the app was not answering
        self._lock = threading.Lock()

    # -- the app's own lifecycle -------------------------------------------------------------------
    def surface(self):
        """`app.py:317` writes native-control.json at startup; `:794` unlinks it on shutdown."""
        path = self.owner / 'native-control.json'
        path.write_text(json.dumps({'port': 50000, 'credential': 'x'}))
        path.chmod(0o600)
        return path

    def scope(self):
        """`round14_bootstrap_compile.py:242-244` -- written BEFORE the compile is requested (:275)."""
        path = self.round_dir / 'current-scope.json'
        path.write_text(json.dumps({'run_id': RUN_ID,
                                    'deployment_id': '5d0e3c1a-0000-4000-8000-000000000001'}))
        path.chmod(0o600)
        return path

    def compile_and_staging_done(self):
        """Step 1 + the compile are over when this phase begins, and the compile left its own marks."""
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.compile_marker.parent.mkdir(parents=True, exist_ok=True)
        self.compile_marker.write_text(json.dumps({'protocol': 'mycelium.operator_plan.v1'}))
        # This phase starts after compilation has released app.guard. Supply the
        # producer's post-guard identity, not merely its earlier workflow plan.
        from mycelium_a13_macos.authority import _atomic_private_json
        identity = {'run_id': RUN_ID,
                    'deployment_id': '5d0e3c1a-0000-4000-8000-000000000001',
                    'epoch': 0}
        _atomic_private_json(self.run_root / 'compile-start.private.json',
                             {'protocol': 'mycelium.chain_compile_start.v1', **identity})
        _atomic_private_json(self.run_root / 'compile-complete.private.json',
                             {'protocol': 'mycelium.chain_compile_complete.v1',
                              **identity, 'state': 'compiled'})
        # `serving.py:596-598` creates these at COMPILE time (`prepare_registry_launch`), so their
        # presence is NOT evidence that the serving phase ended.
        self.serving_root.mkdir(parents=True, exist_ok=True)
        (self.serving_root / 'product').mkdir(parents=True, exist_ok=True)
        return self

    def _qualify(self):
        """The app's own qualification thread: the route opens, the model loads, the registry persists."""
        _REAL_SLEEP(self.qualify_seconds)
        # `mycelium_live/registry.py:149` -> `_persist` (`:785-815`): the app's durable record of the
        # registry it is about to serve, written BEFORE it reports `api_running` (workflow.py:213).
        self.serving_marker.write_text(json.dumps(
            {'protocol': 'mycelium.live_deployment_registry.v1',
             'selected_deployment_id': '5d0e3c1a-0000-4000-8000-000000000001'}))
        with self._lock:
            self.events.append('serving_evidence_completed')
            self.qualifying = False
            self.answering = True
            self.serving = 'api_running'

    # -- the app's own reports ----------------------------------------------------------------------
    def _report(self):
        with self._lock:
            self.calls.append(STATUS)
            if self.serving == 'staging':
                self.staging_reads += 1
                if self.staging_reads >= 2:
                    self.serving = 'awaiting_activation'
            state = self.serving
        return {'compilation': {'state': 'compiled', 'execution_proposal': dict(PROPOSAL)},
                'serving': None if state is None else {'state': state}}

    def _refuse(self, code):
        raise RuntimeError('local_api_http_409:%s' % code)

    def _start_serving(self):
        """`chain_compile.py:89-100`: the serve thread starts; the app is now busy qualifying."""
        with self._lock:
            self.serving = 'starting'
            self.qualifying = True
            self.answering = False
            self.activations += 1
            self.events.append('activated')
        threading.Thread(target=self._qualify, name='simulated-qualification', daemon=True).start()
        return {'state': 'activation_requested', 'route_ready': False}

    def call(self, path, body=None):
        with self._lock:
            if path != STATUS:
                self.calls.append(path)
            answering = self.answering
            state = self.serving
        if not answering:
            # A real call would expire inside `app_rpc.py:15`'s fixed read deadline too.
            raise TimeoutError('timed out')
        if path == STATUS:
            return self._report()
        if path == START:
            with self._lock:
                self.serving = 'staging'
            return {'state': 'starting', 'registry_origin': 'http://127.0.0.1:62346/', 'route_ready': False}
        if path == ACTIVATE:
            if state != 'awaiting_activation':
                self._refuse('managed_workflow_not_awaiting_activation')
            return self._start_serving()
        if path in ('/api/staging/check', '/api/staging/cleanup/check'):
            published = state is not None
            return {'offer': {'offer_id': 'offer-1', 'archive_digest': 'sha256:' + 'ab' * 32} if published else None}
        if path == '/api/work/preview':
            if state is None:
                self._refuse('work_offer_missing')
            return {'consent_digest': 'sha256:' + 'cd' * 32}
        if path == '/api/work/accept':
            return {'state': 'running', 'error_code': None, 'route_ready': False}
        if path == '/api/status':
            return {'state': 'ok'}
        return {'ok': path}

    def client_factory(self, root):
        """Stand in for `app_rpc.AppClient(root)`: the app's surface exists, the app does not answer."""
        with self._lock:
            self.handshake_attempts += 1
            busy = not self.answering
            self.events.append('handshake_attempt' if busy else 'handshake_answered')
            if busy:
                self.handshake_refusals += 1
        if busy:
            raise TimeoutError('timed out')          # app_rpc.py:15 -- the p82r1/p82r3/p83r1 shape
        return _Client(self)

    # `round14_rpc` calls the class, not a factory; both names point at the same stand-in.
    app_client = client_factory

    def once(self, path, body=None):
        """Stand in for the remote `_target_once(path, body)` ssh hop."""
        return self.call(path, body)


class _Client:
    def __init__(self, app):
        self.app = app

    def call(self, path, body=None):
        return self.app.call(path, body)


@pytest.fixture()
def transport(tmp_path, monkeypatch):
    """Load `round14_rpc` exactly as the pinned step does (same import name, same module object)."""
    owner_root = tmp_path / 'owner-lhr'
    owner_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv('A13_OWNER_ROOT', str(owner_root))
    monkeypatch.setenv('A13_REMOTE_ROOT', str(tmp_path / 'remote'))
    monkeypatch.setenv('A13_TARGET_PREVIEW', 'preview83')
    monkeypatch.setenv('A13_RPC_ROUND', 'rpc-round83')
    # Compressed clocks: the budgets' SHAPE is unchanged, only their size is scaled down so the
    # simulation of a multi-minute qualification runs in about a second. The real values are asserted
    # unchanged by the unit tests below and by tests/a13_operator/test_activation_readiness_sequencing.py.
    monkeypatch.setenv('A13_HANDSHAKE_DEADLINE_S', str(HANDSHAKE_BUDGET_S))
    monkeypatch.setenv('A13_HANDSHAKE_RETRY_SLEEP_S', '0.02')
    monkeypatch.setenv('A13_READINESS_RETRY_SLEEP_S', '0.02')
    monkeypatch.syspath_prepend(str(OPERATOR))
    for name in ('round14_rpc', 'round14_start_and_activate'):
        sys.modules.pop(name, None)
    module = importlib.import_module('round14_rpc')
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: _REAL_SLEEP(min(seconds, 0.02)))
    yield module, owner_root
    for name in ('round14_rpc', 'round14_start_and_activate'):
        sys.modules.pop(name, None)


def _app(owner_root, module, *, qualify_seconds=QUALIFY_SECONDS):
    """The owner's on-disk state as step 1's compile leaves it when this phase begins."""
    app = QualifyingApp(owner_root, module.PRIVATE, qualify_seconds=qualify_seconds)
    app.surface()
    app.scope()
    app.compile_and_staging_done()
    return app


def test_completed_compile_fixture_has_current_post_guard_evidence(transport):
    """Serving-phase tests begin only after the real compile completion contract."""
    module, owner_root = transport
    app = _app(owner_root, module)
    assert module._compile_evidence(RUN_ID) == 'completed'
    assert app.answering is True


def _drive_the_pinned_step(monkeypatch, rpc, app):
    """Run the PINNED `round14_start_and_activate.main()` against the scripted app.

    Only the transport seam is scripted (`_client`, the remote ssh hop, the surface checks); every
    sequencing and waiting decision is the code under test.
    """
    rpc._OWNER_ANSWERED.clear()
    rpc._AWAKE.clear()
    rpc._TARGET_SURFACE_CHECKED.clear()
    if hasattr(rpc, '_SERVING_REQUESTED'):
        rpc._SERVING_REQUESTED.clear()
    monkeypatch.setattr(rpc, 'require_local', lambda root, *a, **k: {'present': True})
    monkeypatch.setattr(rpc, 'require_remote', lambda *args, **kwargs: None)
    # ONLY the innermost client is scripted: `_client`'s bounded retry, `_busy_reason` and
    # `HandshakeTimeout` are the real code, exactly as they are in a round.
    monkeypatch.setattr(rpc, 'AppClient', app.app_client)
    monkeypatch.setattr(rpc, '_target_once', app.once)
    step = importlib.import_module('round14_start_and_activate')
    step.main()
    return step


def _fake_clock(monkeypatch, module):
    """A clock that advances only when the code sleeps, so 'did it wait?' is directly measurable.
    The poll interval is taken from the transport's own env knob and set to the whole bound, so the
    bounded cases end after one observed poll instead of ninety thousand.
    """
    monkeypatch.setenv('A13_READINESS_RETRY_SLEEP_S', str(module._READINESS_BUDGET_S))
    clock = {'t': 0.0}
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock['t'])
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: clock.__setitem__('t', clock['t'] + seconds))
    return clock


# ───────────────────────── the wall, and the round surviving it ─────────────────────────

def test_the_round_survives_a_qualification_that_outlasts_the_handshake_budget(monkeypatch, transport):
    """GREEN: the app cannot answer while it qualifies; the round must not spend its budget asking.

    The app answers nothing until its own registry record exists, so the assertions are about the
    transport: the round reached the terminal `api_running` state, it was asked NOTHING while the app
    qualified, and the first thing it asked after activation came after the app's own evidence.
    """
    module, owner_root = transport
    app = _app(owner_root, module)

    _drive_the_pinned_step(monkeypatch, module, app)

    assert app.activations == 1, 'the round reached activation'
    assert app.serving == 'api_running', 'and the app reached the state the step waits for'
    assert app.handshake_refusals == 0, 'no RPC was attempted while the app could not answer'
    events = app.events        # every handshake, in order, and the moment this round asked for activation
    assert events.index('serving_evidence_completed') < events.index('handshake_answered', events.index('activated')), \
        'the app was asked only after its own evidence said the serving phase had ended'


def test_the_observation_costs_the_round_no_transport_attempt(monkeypatch, transport):
    """The measurable difference: the wait itself issues zero RPCs, and it really does wait."""
    module, owner_root = transport
    app = _app(owner_root, module)
    threading.Thread(target=app._qualify, name='simulated-qualification', daemon=True).start()
    app.qualifying, app.answering = True, False

    started = time.monotonic()
    evidence = module.await_serving_end()
    elapsed = time.monotonic() - started

    assert evidence == 'completed'
    assert app.handshake_attempts == 0, 'the wait issued no RPC'
    assert elapsed >= QUALIFY_SECONDS / 2, 'the wait really waited for the app, rather than returning at once'


def test_without_the_observational_wait_the_same_qualification_ends_the_round(monkeypatch, transport):
    """RED witness: the SAME pinned step with the observational wait disabled -- the pre-fix transport.

    The app is alive, its surface is present, and the round still ends by `handshake_timeout` inside
    the post-activation status read (`round14_start_and_activate.py:43`). Kept so the behaviour that
    was removed cannot return unnoticed.
    """
    module, owner_root = transport
    app = _app(owner_root, module, qualify_seconds=30.0)      # never finishes: it never answers

    monkeypatch.setattr(module, 'await_serving_end', lambda: 'absent', raising=False)   # pre-fix transport

    with pytest.raises(RuntimeError) as excinfo:
        _drive_the_pinned_step(monkeypatch, module, app)

    assert 'handshake_timeout' in str(excinfo.value), \
        'the pre-fix round dies inside the post-activation status read'
    assert app.handshake_refusals > 0, \
        'the pre-fix round spent its whole bounded budget asking an app that could not answer'
    assert app.activations == 1, 'and it had already reached activation'


# ───────────────────────── the evidence itself ─────────────────────────

def test_the_marker_is_the_apps_own_write_before_it_reports_api_running(transport):
    """The marker is not invented here: it is the registry's own persist on the way to `api_running`.

    Checked against the app's own source, so the operator cannot drift from the app: the registry is
    built with that `state_path`, its constructor persists LAST, and `on_started` -- which sets
    `api_running` (`workflow.py:213`) -- runs only after the HTTP server exists.
    """
    module, _ = transport
    serving = (REPO / 'mycelium_a13_macos/serving.py').read_text()
    assert 'registry_state = root / "registry-state.json"' in serving
    assert 'private_state_root: str | Path' in serving          # serving.py:572 -- it is the caller's
    workflow = (REPO / 'mycelium_a13_macos/workflow.py').read_text()
    assert "root=_private_directory(authority.state_root/'serving-runs'/run_id,create=True)" in workflow
    assert "private_state_root=root/'registry'" in workflow, \
        'workflow.py:362 hands the registry this run-dir'
    registry = (REPO / 'mycelium_live/registry.py').read_text()
    assert 'self._state_path = None if state_path is None else Path(state_path)' in registry
    assert registry.index('self._persist()') > registry.index('def __init__'), \
        'the constructor persists (registry.py:149)'
    supervisor = (REPO / 'mycelium_live/supervisor.py').read_text()
    assert supervisor.index('registry = LiveDeploymentRegistry(') < supervisor.index('server = create_server('), \
        'supervisor.py:2287 -- the registry (and its persist) precedes the server'
    assert supervisor.index('server = create_server(') < supervisor.index('on_started(dict(observed))'), \
        'supervisor.py:2427 -- and the started callback is last'
    assert workflow.index("def started(document):") < workflow.index("self._state='api_running'"), \
        'api_running is the callback the server itself invokes'

    sys.path.insert(0, str(REPO))
    managed_control = importlib.import_module('mycelium_a13_macos.managed_control')
    assert managed_control._segment(RUN_ID) == RUN_ID, \
        "the serving-runs directory name IS the run id for this round's identifier shape"

    assert module._serving_marker_path(RUN_ID) == \
        (module.OWNER/'authority'/'serving-runs'/RUN_ID/'registry'/'registry-state.json')
    assert module._serving_error_path(RUN_ID) == module.OWNER/'chain-runs'/RUN_ID/'serve-error.private.json'


def _assert_serving_guard_partition(source):
    """Reject a serving-worker guard or a blocking wait while retiring under one."""
    sites = []
    class GuardVisitor(ast.NodeVisitor):
        scopes: tuple[str, ...] = ()

        def visit_FunctionDef(self, node):
            previous = self.scopes
            self.scopes = (*previous, node.name)
            self.generic_visit(node)
            self.scopes = previous

        def visit_With(self, node):
            if any(isinstance(item.context_expr, ast.Attribute)
                   and isinstance(item.context_expr.value, ast.Name)
                   and item.context_expr.value.id == 'app'
                   and item.context_expr.attr == 'guard' for item in node.items):
                sites.append((self.scopes, node.lineno))
                assert self.scopes[0] in {'start_compile', 'stop_serving'}, \
                    f'app.guard in serving path: {self.scopes}'
                assert 'serve' not in self.scopes, 'serving worker cannot hold app.guard'
                if self.scopes[0] == 'stop_serving':
                    assert not any(isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and call.func.attr in {'join', 'wait', 'run'}
                        for stmt in node.body for call in ast.walk(stmt)), \
                        'retirement may not wait for a worker under app.guard'
            self.generic_visit(node)

    GuardVisitor().visit(ast.parse(source))
    assert any(scope == ('start_compile', 'work') for scope, _ in sites)
    assert any(scope == ('stop_serving',) for scope, _ in sites)
    return sites


def test_the_serving_phase_takes_no_app_guard_a_read_would_have_to_wait_on(transport):
    """Compile owns its long guard; serving owns none; retirement never joins under guard.

    The added compile publication guards only validate epoch/state and assign memory;
    retirement's two short critical sections fence epoch/state, while its potentially
    long thread joins are outside the guard. An activated serving worker runs the
    workflow without app.guard, so the operator's status read cannot queue behind
    the model qualification. Inspect AST, not a raw count of compile/retirement sites.
    """
    chain = (REPO / 'mycelium_a13_macos/chain_compile.py').read_text()
    sites = _assert_serving_guard_partition(chain)
    assert {scope[0] for scope, _ in sites} == {'start_compile', 'stop_serving'}
    # Deterministic RED control: a guard injected into the serving worker must fail.
    changed = chain.replace('def serve():\n        try:',
                            'def serve():\n        with app.guard: pass\n        try:', 1)
    assert changed != chain
    with pytest.raises(AssertionError, match='serving path'):
        _assert_serving_guard_partition(changed)
    workflow = (REPO / 'mycelium_a13_macos/workflow.py').read_text()
    assert 'app.guard' not in workflow, 'the workflow never takes the application guard'
    app = (REPO / 'mycelium_a13_macos/app.py').read_text()
    assert 'with self.app.guard:' in app and 'with self.guard:' in app, \
        'the guard exists and is taken by the app surface (app.py:872) -- unchanged by this wait'


def test_a_progress_artifact_is_not_evidence_the_serving_phase_ended(monkeypatch, transport):
    """`registry/` and `registry/product/` appear at COMPILE time (`serving.py:596-598`): not evidence.

    They are written by `prepare_registry_launch` while the compile still holds `app.guard`, i.e. in
    the window the compile fix rejected as a wake-up. Only the registry's own persisted state counts.
    """
    module, owner_root = transport
    app = _app(owner_root, module)          # compile_and_staging_done() already made registry/product
    _fake_clock(monkeypatch, module)

    assert app.serving_root.is_dir() and (app.serving_root / 'product').is_dir()
    assert module._serving_evidence(RUN_ID) is None
    assert module.await_serving_end() == 'expired', \
        'the earlier artifacts of the same directory are not the app saying the phase ended'
    assert app.handshake_attempts == 0, 'and the round asked the app nothing while it waited'


def test_a_failed_serving_phase_is_terminal_evidence_and_ends_the_wait_at_once(monkeypatch, transport):
    """`chain_compile.py:96` -- the app's own failure record, waited on by nobody."""
    module, owner_root = transport
    app = _app(owner_root, module)
    app.serving_error.write_text(json.dumps({'exception_type': 'WorkerError', 'traceback': 'x'}))
    clock = _fake_clock(monkeypatch, module)

    assert module.await_serving_end() == 'failed'
    assert clock['t'] == 0.0, 'a terminal verdict is not waited on'
    assert app.handshake_attempts == 0


def test_the_wait_disengages_when_there_is_no_run_to_watch(monkeypatch, transport):
    """No scope record, or no run root: a round that is not serving is untouched."""
    module, owner_root = transport
    clock = _fake_clock(monkeypatch, module)
    assert module.await_serving_end() == 'absent'          # no scope record at all
    assert clock['t'] == 0.0

    app = QualifyingApp(owner_root, module.PRIVATE)
    app.surface()
    app.scope()                                           # a scope, but no compile has run
    assert not app.run_root.exists()
    assert module.await_serving_end() == 'absent'
    assert clock['t'] == 0.0


def test_the_wait_disengages_when_no_serving_run_was_prepared(monkeypatch, transport):
    """The wait watches a *PREPARED* serving run: without one it steps aside, like the compile wait does.

    `workflow.py:361-362` -> `serving.py:596-598` creates `{serving-runs}/{run_id}/registry/` (and its
    `product/`) while the compile still holds, so any round that can be activated has it. A run with no
    such root has nothing to observe: 'absent', and the caller's existing read path decides.
    """
    module, owner_root = transport
    clock = _fake_clock(monkeypatch, module)
    app = QualifyingApp(owner_root, module.PRIVATE)
    app.surface()
    app.scope()
    app.run_root.mkdir(parents=True, exist_ok=True)              # a run root ...
    app.compile_marker.parent.mkdir(parents=True, exist_ok=True)  # ... and a compile that ended ...
    app.compile_marker.write_text('{}')
    assert not app.serving_root.exists()                          # ... but no prepared serving run

    assert module.await_serving_end() == 'absent'
    assert clock['t'] == 0.0, 'nothing to observe is not waited on'


def test_a_lost_surface_ends_the_wait_so_the_named_failure_still_raises(monkeypatch, transport):
    """If the app goes away mid-serving the round must fail by NAME (`surface_lost`), not sit for 1800 s."""
    module, owner_root = transport
    app = _app(owner_root, module)
    (owner_root / 'native-control.json').unlink()          # app.py:794 -- the app is not running
    clock = _fake_clock(monkeypatch, module)

    assert module.await_serving_end() == 'absent'
    assert clock['t'] == 0.0, 'a dead app is not waited on'


def test_the_wait_is_bounded_by_the_apps_own_declared_wall_bound(monkeypatch, transport):
    """Bounded, not infinite -- and bounded by a value that already exists, not a new one."""
    module, owner_root = transport
    app = _app(owner_root, module)
    clock = _fake_clock(monkeypatch, module)

    assert module.await_serving_end() == 'expired'
    assert clock['t'] == module._READINESS_BUDGET_S, \
        "the wait ends on the app's own declared wall bound (serving.py RESOURCE_PROPOSAL.max_wall_seconds)"
    assert app.handshake_attempts == 0


def test_the_wait_can_outlast_the_handshake_budget_by_the_apps_own_declaration(transport):
    """The inequality the measured wall violates: a qualification may take longer than a handshake budget."""
    module, _ = transport
    handshake = float(module.os.environ.get('A13_HANDSHAKE_DEADLINE_S', '180'))
    assert module._readiness_budget_s() > handshake, \
        "the observational wait is the app's own 1800 s wall bound; the handshake retry stays what it was"


# ───────────────────────── WHERE the wait engages -- and where it must not ─────────────────────────

def test_the_wait_engages_only_after_this_process_asked_for_activation(monkeypatch, transport):
    """Scoped on purpose: before activation there IS no serving phase to observe, so nothing changes.

    The pre-activation reads are the app's readiness report (`await_ready`'s own probe) and the pinned
    step's first read: gating those would delay a round that has not asked the app to activate at all.
    """
    module, owner_root = transport
    app = _app(owner_root, module)
    assert module._SERVING_DEPENDENT == frozenset((STATUS,)), 'the status read is what this phase blocks'
    assert START not in module._SERVING_DEPENDENT and ACTIVATE not in module._SERVING_DEPENDENT
    assert '/api/quit' not in module._SERVING_DEPENDENT, 'shutdown must not wait for a serving phase'
    assert '/api/serving/stop' not in module._SERVING_DEPENDENT
    assert module._SERVING_REQUESTS == frozenset((ACTIVATE,))

    waited = []
    monkeypatch.setattr(module, 'await_serving_end', lambda: waited.append(True) or 'completed')
    monkeypatch.setattr(module, 'AppClient', app.app_client)
    module._SERVING_REQUESTED.clear()
    assert module.owner(STATUS)['serving'] is None, 'before activation the read is issued as it was'
    assert waited == [], 'and the serving wait does not engage for it'

    module._SERVING_REQUESTED.append(True)     # the same process has now asked the app to activate
    module.owner(STATUS)
    assert waited == [True], 'after activation the status read waits on the app\'s own evidence'


def test_the_activation_call_is_what_marks_this_process(monkeypatch, transport):
    """The trigger is the round's OWN activation request, not a guess about the app's state."""
    module, owner_root = transport
    app = _app(owner_root, module)
    monkeypatch.setattr(module, 'AppClient', app.app_client)
    module._SERVING_REQUESTED.clear()

    module.owner(START, {'execution_proposal': dict(PROPOSAL), 'model_execution_acknowledged': True})
    assert module._SERVING_REQUESTED == [], 'starting is not activating'

    module.owner(ACTIVATE, {'execution_proposal': dict(PROPOSAL), 'activation_acknowledged': True})
    assert module._SERVING_REQUESTED == [True], 'the activation request marks this process'
    assert app.activations == 1


def test_no_existing_budget_window_or_guard_was_changed_by_this_wait(transport):
    """The laws: the transport's own budgets and the two pinned files are untouched."""
    module, _ = transport
    assert module._OWNER_CALL_BUDGET_S == 120.0
    assert module._OWNER_STARTUP_GRACE_S == 20.0
    assert module._READINESS_BUDGET_S == 1800.0
    assert module._READINESS_RETRY_S == 5.0
    assert set(module._READINESS) == {START, ACTIVATE}, \
        'the readiness gate still does not gate the read it uses'
    app_rpc = (OPERATOR / 'app_rpc.py').read_text()
    assert app_rpc.count('timeout=10') == 2, "app_rpc.py's fixed 10 s read deadline is untouched"
    step = (OPERATOR / 'round14_start_and_activate.py').read_text()
    assert "owner('/api/serving/status',{})" in step, 'the pinned step still polls the app itself'
    assert 'await_serving_end' not in step, 'and the wait does not live in the pinned step'


def test_a_segment_invalid_run_id_never_becomes_a_path(monkeypatch, transport):
    """The scope record is the round's own, but a run id is still validated before it is a path."""
    module, _ = transport
    path = module.PRIVATE / 'current-scope.json'
    path.write_text(json.dumps({'run_id': '../../etc/passwd'}))
    assert module._compile_run_id() is None
    path.write_text(json.dumps({'run_id': RUN_ID}))
    assert module._compile_run_id() == RUN_ID


# ───────────────── the read path this must not have changed, re-asserted here ─────────────────
# `tests/a13_macos/test_operator_handshake_retry.py:48-78` pins both behaviours below, but it lives
# under `tests/a13_macos`, which this lane was told not to run. The contract is therefore re-asserted
# here, where the new wait could actually reach it: with no activation requested there is nothing to
# observe, so the read path must be exactly the one that suite measured.

def test_a_busy_app_before_activation_keeps_the_retry_then_use_semantics(monkeypatch, transport):
    """The busy read is still retried (not abandoned) when this process has not asked for activation."""
    module, owner_root = transport
    _surface(owner_root, module)
    seen = {'n': 0}

    class Flaky:
        def __init__(self, root):
            seen['n'] += 1
            if seen['n'] < 3:
                raise TimeoutError('timed out')
            self.root = root

        def call(self, path, body=None):
            return {'ok': path}

    module._SERVING_REQUESTED.clear()
    monkeypatch.setattr(module, 'AppClient', Flaky)
    assert module.owner(STATUS) == {'ok': STATUS}
    assert seen['n'] == 3, 'the handshake must still be retried, not abandoned'


def test_a_persistently_busy_app_before_activation_still_fails_by_name(monkeypatch, transport):
    """And a permanently busy app still ends the read, bounded, by its own name."""
    module, owner_root = transport
    _surface(owner_root, module)

    class AlwaysBusy:
        def __init__(self, root):
            raise TimeoutError('timed out')

    module._SERVING_REQUESTED.clear()
    monkeypatch.setattr(module, 'AppClient', AlwaysBusy)
    with pytest.raises(module.HandshakeTimeout) as excinfo:
        module.owner(STATUS)
    assert 'handshake_timeout' in str(excinfo.value)
    assert str(module.OWNER) in str(excinfo.value)


def _surface(owner_root, module):
    """`app.py:317` -- the surface the read path needs before it is even attempted."""
    path = pathlib.Path(owner_root) / 'native-control.json'
    path.write_text(json.dumps({'port': 50000, 'credential': 'x'}))
    path.chmod(0o600)
    return path
