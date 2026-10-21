# SPDX-License-Identifier: AGPL-3.0-only
"""The compile wait is OBSERVATIONAL: the round reads the app's OWN on-disk evidence, not an HTTP poll.

THE WALL (measured; quoted from the rounds that recorded it). `p82r1`/`p82r3` died inside the FIRST
`/api/serving/status` read of `round14_start_and_activate.py:12` as
`handshake_timeout:...:attempts=16:TimeoutError('timed out')` while the app was ALIVE -- its surface
stayed present and it answered `/api/status` and `/api/quit` minutes later
(`tests/a13_operator/test_activation_readiness_sequencing.py:3-9`). The compile takes minutes; while it
runs, EVERY request -- including a cheap status read -- expires inside `app_rpc.py:15`'s FIXED 10 s read
deadline, and `_client`'s busy retry is itself bounded, so the round dies minutes before the compile can
finish. `p83r1` died at the same line, i.e. before any control call was ever issued.

WHAT THE APP ACTUALLY WRITES, AND WHEN (read off source, not guessed):

  * compile START -- `mycelium_a13_macos/chain_compile.py:31` creates the run root
    `{app.root}/chain-runs/{run_id}`; `:39`, `:43`, `:44` write `template.json`, `topology.json`,
    `observations.json`; `:32` sets `{'state':'compiling'}` IN MEMORY ONLY (never persisted);
  * compile BODY -- `scripts/build_qwen_live_route.py:1341-2158` writes `compiled/transfer-bundle/...`,
    the stage packs (`:1692`, `:1752`), `compiled/control/*.json` (`:1888-1904`), the local challenge
    checkpoint (`:1870`), then `compiled/operator-plan.json` (`:2084`) and `compiled/build-report.json`
    (`:2157`) -- the last write of `build()`;
  * compile END (success) -- `workflow.py` publishes an EARLY operator-plan marker inside
    `app.guard`; `chain_compile.py` sets compiled state, releases that guard, then atomically
    publishes private `chain-runs/{run_id}/compile-complete.private.json`.
  * compile END (failure) -- private `compile-error.private.log`, not a success marker.

Only the private post-guard completion matched to the start record, scoped run/deployment and
compiler epoch is successful observational evidence. The early operator-plan file alone is not.
step 1 writes `{A13_OWNER_ROOT}/{A13_RPC_ROUND}/current-scope.json` with the run id
(`round14_bootstrap_compile.py:242-244`) before it requests the compile (`:275`), and both steps run
under one `A13_RPC_ROUND` (`run_a13_attempt.sh:34`, `:49-50`).

WHY `build-report.json` IS NOT THE WAKE-UP, even though `build()` writes it last: after it,
`chain_compile.py:54` takes `app.guard` and runs `prepare_serving_workflow`, which can spend up to
`_BOOT_ID_OBSERVATION_BUDGET_SECONDS=180` in a bounded retry (`workflow.py:38`, `:316-330`) while
holding that guard -- and every session-taking request, including the handshake POST `/native-session`
(`app.py:849-876`, `with self.app.guard` at `:872`), blocks on the same guard. Waking the round there
would put its first read inside a window as long as the very budget it must survive.

WHAT THESE TESTS PIN.

  * the round SURVIVES a compile that outlasts the handshake budget, and it does so by issuing NO RPC
    at all while the app is unresponsive (the `handshake_attempts == 0` assertions);
  * the pre-fix transport -- the same pinned step with the observational wait disabled -- still dies by
    `handshake_timeout`, kept as a witness so the defect cannot return unnoticed;
  * the early marker is not a terminal verdict; only the post-guard completion matched to
    the scoped run/deployment and compiler epoch can wake the observer;
  * the wait is bounded by the app's OWN declared wall bound and reuses the existing constants:
    no timeout, deadline, window or retry count is changed.

RUN RED (the pre-fix tree, this file added but `await_compile_end` absent):
    env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV /opt/homebrew/bin/python3 -B -m pytest -q \
        -p no:cacheprovider tests/a13_operator/test_compile_evidence_wait.py
"""

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
COMPILE_SECONDS = 2.0                         # a compressed compile: the real one measured 7 m 46 s
HANDSHAKE_BUDGET_S = 0.5                      # a compressed `A13_HANDSHAKE_DEADLINE_S` (real default 180)
# The wall's defining inequality, kept in the simulation: the compile OUTLASTS the handshake budget, so
# the round cannot survive by retrying -- it has to stop asking. (Measured: 466 s vs 180 s.)
assert COMPILE_SECONDS > HANDSHAKE_BUDGET_S
STATUS = '/api/serving/status'
START = '/api/serving/start'
ACTIVATE = '/api/serving/activate'


class CompilingApp:
    """The app WHILE IT COMPILES: its control surface answers nothing, its disk keeps speaking.

    Two halves, both taken from the app's own source rather than invented:

    * the transport half -- `app_client` stands in for `app_rpc.AppClient` ONLY: while the compile runs
      it raises the exact shape the real rounds recorded, a bare `TimeoutError('timed out')` out of
      `app_rpc.py:15`'s fixed deadline. Everything above it is the real transport: `_client`'s bounded
      busy retry, `_busy_reason`'s classification, and the `HandshakeTimeout` that ends the round.
    * the disk half -- `start_compile()` creates the run root and start record; `_compile` writes
      the early workflow plan, then the completion only after it stops blocking the app.
    """

    def __init__(self, owner_root, round_dir, *, compile_seconds=COMPILE_SECONDS,
                 hold_after_marker=False):
        self.owner = pathlib.Path(owner_root)
        self.round_dir = pathlib.Path(round_dir)
        self.run_root = self.owner / 'chain-runs' / RUN_ID
        self.marker = self.owner / 'authority' / 'serving-runs' / RUN_ID / 'operator-plan.json'
        self.completion = self.run_root / 'compile-complete.private.json'
        self.observed_held = threading.Event()
        self.compile_seconds = compile_seconds
        self.hold_after_marker = hold_after_marker
        self._marker_handshake = threading.Event()
        self.compiling = False
        self.events = []
        self.calls = []
        self.handshake_attempts = 0      # every `app_rpc.AppClient(root)` construction
        self.handshake_refusals = 0      # ... and those made while the app was not answering
        self.serving = None
        self.staging_reads = 0
        self.activations = 0
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

    def start_compile(self):
        """`chain_compile.py:31-44`: the run root and its START records, then the compile thread."""
        self.run_root.mkdir(parents=True, exist_ok=True)
        (self.run_root / 'template.json').write_text('{}')
        (self.run_root / 'compile-start.private.json').write_text(json.dumps({
            'protocol':'mycelium.chain_compile_start.v1','run_id':RUN_ID,
            'deployment_id':'5d0e3c1a-0000-4000-8000-000000000001','epoch':0}))
        (self.run_root / 'compile-start.private.json').chmod(0o600)
        self.compiling = True
        threading.Thread(target=self._compile, name='simulated-chain-compile', daemon=True).start()

    def _compile(self):
        _REAL_SLEEP(self.compile_seconds)
        # Workflow publishes the plan under app.guard; this is not post-guard evidence.
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        self.marker.write_text(json.dumps({'protocol': 'mycelium.operator_plan.v1'}))
        # The real workflow writes this inside app.guard, BEFORE registry preparation,
        # approval construction and the in-memory 'compiled' state. Hold that ordering
        # until the first post-marker handshake witnesses the still-busy app.
        if self.hold_after_marker:
            if not self._marker_handshake.wait(timeout=15):
                raise AssertionError('post-marker observation was never observed')
        with self._lock:
            self.events.append('evidence_completed')
            self.compiling = False
        self.completion.write_text(json.dumps({
            'protocol':'mycelium.chain_compile_complete.v1','run_id':RUN_ID,
            'deployment_id':'5d0e3c1a-0000-4000-8000-000000000001',
            'epoch':0,'state':'compiled'}))
        self.completion.chmod(0o600)

    # -- the app's own reports -----------------------------------------------------------------------
    def _report(self):
        with self._lock:
            self.calls.append(STATUS)
            if self.serving == 'staging':
                self.staging_reads += 1
                if self.staging_reads >= 2:
                    self.serving = 'awaiting_activation'
            elif self.serving == 'starting':
                self.serving = 'api_running'
            state = self.serving
        return {'compilation': {'state': 'compiled', 'execution_proposal': dict(PROPOSAL)},
                'serving': None if state is None else {'state': state}}

    def _refuse(self, code):
        raise RuntimeError('local_api_http_409:%s' % code)

    def call(self, path, body=None):
        with self._lock:
            if path != STATUS:
                self.calls.append(path)
            state = self.serving
        if path == STATUS:
            return self._report()
        if path == START:
            self.serving = 'staging'
            return {'state': 'starting', 'registry_origin': 'http://127.0.0.1:62346/', 'route_ready': False}
        if path == ACTIVATE:
            if state != 'awaiting_activation':
                self._refuse('managed_workflow_not_awaiting_activation')
            self.serving = 'starting'
            self.activations += 1
            return {'state': 'activation_requested', 'route_ready': False}
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
            busy = self.compiling
            self.events.append('handshake_attempt' if busy else 'handshake_answered')
        if busy:
            with self._lock:
                self.handshake_refusals += 1
            if self.hold_after_marker and self.marker.exists():
                self._marker_handshake.set()
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
    # simulation of a multi-minute compile runs in about a second. The real values are asserted
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


def _app(owner_root, module, *, compile_seconds=COMPILE_SECONDS, hold_after_marker=False):
    """The owner's on-disk state as step 1 leaves it when step 2 begins."""
    app = CompilingApp(owner_root, module.PRIVATE, compile_seconds=compile_seconds,
                       hold_after_marker=hold_after_marker)
    app.surface()
    app.scope()
    app.start_compile()
    return app


def _drive_the_pinned_step(monkeypatch, rpc, app):
    """Run the PINNED `round14_start_and_activate.main()` against the scripted app.

    Only the transport seam is scripted (`_client`, the remote ssh hop, the surface checks); every
    sequencing and waiting decision is the code under test.
    """
    rpc._OWNER_ANSWERED.clear()
    rpc._AWAKE.clear()
    rpc._TARGET_SURFACE_CHECKED.clear()
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

def test_the_round_survives_a_compile_that_outlasts_the_handshake_budget(monkeypatch, transport):
    """GREEN: the compile is still running when step 2 starts; the round must not spend its budget.

    The app answers nothing until its own completion exists, so the assertions concern transport:
    activation succeeds without asking the app while the producer still holds the guard.
    """
    module, owner_root = transport
    app = _app(owner_root, module)

    _drive_the_pinned_step(monkeypatch, module, app)

    assert app.activations == 1, 'the round reached activation'
    assert app.handshake_refusals == 0, 'no RPC was attempted while the app could not answer'
    assert app.events.index('evidence_completed') < app.events.index('handshake_answered'), \
        'the app was asked only after its own evidence said the compile had ended'


def test_marker_published_before_compile_unlock_is_not_a_finished_compile(monkeypatch, transport):
    """Force the real ordering: marker, still-held guard, then compiled state."""
    module, owner_root = transport
    app = _app(owner_root, module, hold_after_marker=True)
    original = module._compile_evidence
    def observed(run_id):
        result = original(run_id)
        if app.marker.exists() and result is None:
            app.observed_held.set()
            app._marker_handshake.set()  # observation releases both buggy and fixed paths
        return result
    monkeypatch.setattr(module, '_compile_evidence', observed)

    _drive_the_pinned_step(monkeypatch, module, app)

    assert app.observed_held.is_set() or app.handshake_refusals > 0, 'the post-marker interval was exercised'
    assert app._marker_handshake.is_set(), 'the held post-marker interval was released'
    assert app.activations == 1
    assert app.handshake_refusals == 0, 'no RPC while the compile still holds the app guard'


def test_the_observation_costs_the_round_no_transport_attempt(monkeypatch, transport):
    """The measurable difference: the wait itself issues zero RPCs, and it really does wait."""
    module, owner_root = transport
    app = _app(owner_root, module)

    started = time.monotonic()
    evidence = module.await_compile_end()
    elapsed = time.monotonic() - started

    assert evidence == 'completed'
    assert app.handshake_attempts == 0, 'the wait issued no RPC'
    assert elapsed >= COMPILE_SECONDS / 2, 'the wait really waited for the app, rather than returning at once'


def test_without_the_observational_wait_the_same_compile_ends_the_round(monkeypatch, transport):
    """RED witness: the SAME pinned step with the observational wait disabled -- the pre-fix transport.

    This is the p82r1/p82r3/p83r1 death at `round14_start_and_activate.py:12`, reproduced: the app is
    alive, its surface is present, and the round still ends by `handshake_timeout` minutes before the
    compile could finish. Kept so the behaviour that was removed cannot return unnoticed.
    """
    module, owner_root = transport
    app = _app(owner_root, module, compile_seconds=30.0)      # never finishes: it never answers

    monkeypatch.setattr(module, 'await_compile_end', lambda: 'absent', raising=False)   # pre-fix transport

    with pytest.raises(RuntimeError) as excinfo:
        _drive_the_pinned_step(monkeypatch, module, app)

    assert 'handshake_timeout' in str(excinfo.value), \
        'the pre-fix round dies inside the first status read, before any control call'
    assert app.handshake_refusals > 0, \
        'the pre-fix round spent its whole bounded budget asking an app that could not answer'
    assert app.calls == [], 'the app answered nothing at all while compiling'
    assert app.activations == 0, 'and no control call was ever issued'


# ───────────────────────── the evidence itself ─────────────────────────

def test_early_marker_precedes_compiled_state_and_is_not_completion(transport):
    """The workflow plan is a real early artifact, not the compiler's post-guard completion.

    The serving-runs directory uses the run id (`managed_control._segment`), while
    chain_compile publishes completion in the separately scoped chain-runs root.
    """
    module, _ = transport
    workflow = (REPO / 'mycelium_a13_macos/workflow.py').read_text()
    assert "root=_private_directory(authority.state_root/'serving-runs'/run_id,create=True)" in workflow
    assert "path=root/'operator-plan.json'" in workflow
    # The workflow is built (and handed back) only after the marker is written.
    built = 'workflow=ManagedServingWorkflow('
    assert built in workflow and workflow.count(built) == 1
    assert workflow.index("_atomic_private_json(path,bound)") < workflow.index(built)
    assert workflow.index(built) < workflow.index('    return workflow\n')

    chain = (REPO / 'mycelium_a13_macos/chain_compile.py').read_text()
    assert chain.index('prepare_serving_workflow(') < chain.index("{'state':'compiled'"), \
        'the app reports compiled only after the marker exists'

    sys.path.insert(0, str(REPO))
    managed_control = importlib.import_module('mycelium_a13_macos.managed_control')
    assert managed_control._segment(RUN_ID) == RUN_ID, \
        "the serving-runs directory name IS the run id for this round's identifier shape"

    assert module._marker_path(RUN_ID) == (module.OWNER/'authority'/'serving-runs'/RUN_ID/'operator-plan.json')
    assert module._run_root(RUN_ID) == module.OWNER/'chain-runs'/RUN_ID


def test_only_exact_current_start_and_completion_are_success(monkeypatch, transport):
    module, owner_root = transport
    app = CompilingApp(owner_root, module.PRIVATE, compile_seconds=10_000)
    app.surface();app.scope();app.run_root.mkdir(parents=True)
    start={'protocol':'mycelium.chain_compile_start.v1','run_id':RUN_ID,
           'deployment_id':'5d0e3c1a-0000-4000-8000-000000000001','epoch':2}
    complete={'protocol':'mycelium.chain_compile_complete.v1','run_id':RUN_ID,
              'deployment_id':start['deployment_id'],'epoch':2,'state':'compiled'}
    def write(path,doc):path.write_text(json.dumps(doc));path.chmod(0o600)
    write(app.run_root/'compile-start.private.json',start)
    app.marker.parent.mkdir(parents=True)
    app.marker.write_text('{}')
    assert module._compile_evidence(RUN_ID) is None, 'early operator plan is not completion'
    write(app.completion,complete)
    assert module._compile_evidence(RUN_ID)=='completed'
    for change in ({'run_id':'other-run'},{'deployment_id':'other-deployment'},
                   {'epoch':1},{'epoch':'2'},{'state':'failed'},{'extra':'unrecognized'}):
        write(app.completion,{**complete,**change})
        assert module._compile_evidence(RUN_ID) is None, change
    app.completion.write_text('{broken')
    assert module._compile_evidence(RUN_ID) is None
    app.completion.unlink()
    app.completion.symlink_to(app.run_root/'compile-start.private.json')
    assert module._compile_evidence(RUN_ID) is None, 'symlink is not private producer evidence'
    app.completion.unlink()
    write(app.completion,complete)
    write(app.run_root/'compile-start.private.json',{**start,'epoch':3})
    assert module._compile_evidence(RUN_ID) is None, 'superseded epoch rejects an old completion'
    (app.run_root/'compile-start.private.json').unlink()
    assert module._compile_evidence(RUN_ID) is None


def test_progress_artifacts_are_not_evidence_the_compile_ended(monkeypatch, transport):
    """`template.json`/`build-report.json` appear while the compile is STILL running: they must not wake it.

    `build-report.json` is the last write of `build()` (`build_qwen_live_route.py:2157`), but
    `chain_compile.py:54-69` then runs `prepare_serving_workflow` UNDER `app.guard`, with a bounded retry
    of up to 180 s inside it (`workflow.py:38`, `:316-330`) -- and the handshake POST needs that guard
    (`app.py:872`). Waking there would put the round's first read inside a window as long as the budget
    it must survive. The early workflow plan is also not terminal; only post-guard evidence counts.
    """
    module, owner_root = transport
    app = _app(owner_root, module, compile_seconds=10_000.0)
    (app.run_root / 'compiled').mkdir(parents=True, exist_ok=True)
    (app.run_root / 'compiled' / 'build-report.json').write_text('{}')
    _fake_clock(monkeypatch, module)

    assert module._compile_evidence(RUN_ID) is None
    assert module.await_compile_end() == 'expired', \
        'a progress artifact is not the app saying the compile ended'
    assert app.handshake_attempts == 0, 'and the round asked the app nothing while it waited'


def test_a_failed_compile_is_terminal_evidence_and_ends_the_wait_at_once(monkeypatch, transport):
    """`chain_compile.py:71` -- the app's own failure record, waited on by nobody."""
    module, owner_root = transport
    app = _app(owner_root, module, compile_seconds=10_000.0)
    (app.run_root / 'compile-error.private.log').write_text('traceback')
    clock = _fake_clock(monkeypatch, module)

    assert module.await_compile_end() == 'failed'
    assert clock['t'] == 0.0, 'a terminal verdict is not waited on'
    assert app.handshake_attempts == 0


def test_the_wait_disengages_when_there_is_no_run_to_watch(monkeypatch, transport):
    """No scope record, or no run root: a round that is not waiting on a compile is untouched.

    This is what keeps every other caller of `owner()` the behaviour it had: the wait can only engage
    inside a round whose own step 1 has left a compile behind.
    """
    module, owner_root = transport
    clock = _fake_clock(monkeypatch, module)
    assert module.await_compile_end() == 'absent'          # no scope record at all
    assert clock['t'] == 0.0

    app = CompilingApp(owner_root, module.PRIVATE, compile_seconds=10_000.0)
    app.surface()
    app.scope()                                           # a scope, but no compile has started
    assert not app.run_root.exists()
    assert module.await_compile_end() == 'absent'
    assert clock['t'] == 0.0


def test_a_lost_surface_ends_the_wait_so_the_named_failure_still_raises(monkeypatch, transport):
    """If the app goes away mid-compile the round must fail by NAME (`surface_lost`), not sit for 1800 s."""
    module, owner_root = transport
    app = _app(owner_root, module, compile_seconds=10_000.0)
    (owner_root / 'native-control.json').unlink()          # app.py:794 -- the app is not running
    clock = _fake_clock(monkeypatch, module)

    assert module.await_compile_end() == 'absent'
    assert clock['t'] == 0.0, 'a dead app is not waited on'


def test_the_wait_is_bounded_by_the_apps_own_declared_wall_bound(monkeypatch, transport):
    """Bounded, not infinite -- and bounded by a value that already exists, not a new one."""
    module, owner_root = transport
    app = _app(owner_root, module, compile_seconds=10_000.0)
    clock = _fake_clock(monkeypatch, module)

    assert module.await_compile_end() == 'expired'
    assert clock['t'] == module._READINESS_BUDGET_S, \
        "the wait ends on the app's own declared wall bound (serving.py RESOURCE_PROPOSAL.max_wall_seconds)"
    assert app.handshake_attempts == 0


def test_the_wait_can_outlast_the_handshake_budget_by_the_apps_own_declaration(transport):
    """The inequality the measured wall violates: a compile may take longer than a handshake budget."""
    module, _ = transport
    handshake = float(module.os.environ.get('A13_HANDSHAKE_DEADLINE_S', '180'))
    assert module._readiness_budget_s() > handshake, \
        "the observational wait is the app's own 1800 s wall bound; the handshake retry stays what it was"


def test_the_compile_dependent_paths_are_the_ones_the_compile_blocks(transport):
    """Scoped on purpose: an aborted round must still be able to quit promptly."""
    module, _ = transport
    assert {STATUS, START, ACTIVATE} <= set(module._COMPILE_DEPENDENT)
    assert '/api/quit' not in module._COMPILE_DEPENDENT, 'shutdown must not wait for a compile'
    assert '/api/serving/stop' not in module._COMPILE_DEPENDENT


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
    assert 'await_compile_end' not in step, 'and the wait does not live in the pinned step'


def test_a_segment_invalid_run_id_never_becomes_a_path(monkeypatch, transport):
    """The scope record is the round's own, but a run id is still validated before it is a path."""
    module, _ = transport
    path = module.PRIVATE / 'current-scope.json'
    path.write_text(json.dumps({'run_id': '../../etc/passwd'}))
    assert module._compile_run_id() is None
    path.write_text(json.dumps({'run_id': RUN_ID}))
    assert module._compile_run_id() == RUN_ID


# ───────────────── the read path this must not have changed, re-asserted here ─────────────────
# `tests/a13_macos/test_operator_handshake_retry.py:48-78` pins both behaviours below, but it lives under
# `tests/a13_macos`, which this lane was told not to run. The contract is therefore re-asserted here,
# where the new wait could actually reach it: with no round scope there is nothing to observe, so the
# read path must be exactly the one that suite measured.

def test_a_busy_app_with_nothing_to_watch_keeps_the_retry_then_use_semantics(monkeypatch, transport):
    """The busy read is still retried (not abandoned) when the round is not watching a compile."""
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

    monkeypatch.setattr(module, 'AppClient', Flaky)
    assert module.owner(STATUS) == {'ok': STATUS}
    assert seen['n'] == 3, 'the handshake must still be retried, not abandoned'


def test_a_persistently_busy_app_with_nothing_to_watch_still_fails_by_name(monkeypatch, transport):
    """And a permanently busy app still ends the read, bounded, by its own name."""
    module, owner_root = transport
    _surface(owner_root, module)

    class AlwaysBusy:
        def __init__(self, root):
            raise TimeoutError('timed out')

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
